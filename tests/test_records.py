"""Tests for record types (v0.2b)."""

import io
import unittest

from sigil.checker import check
from sigil.errors import CheckError, ParseError
from sigil.interp import Interpreter
from sigil.parser import parse


def run(source: str) -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(""), stdout=out).run_main()
    return out.getvalue()


def check_only(source: str) -> None:
    check(parse(source))


class TestRecordExecution(unittest.TestCase):
    def test_construct_and_access(self):
        out = run("""
            record Point {
                x: Int,
                y: Int,
            }
            fn norm2(p: Point) -> Int {
                return p.x * p.x + p.y * p.y;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let p: Point = Point { x: 3, y: 4 };
                print(console, str(norm2(p)));
            }
        """)
        self.assertEqual(out, "25\n")

    def test_nested_records_and_lists(self):
        out = run("""
            record Point {
                x: Int,
                y: Int,
            }
            record Path {
                name: Text,
                points: List[Point],
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let path: Path = Path {
                    name: "diag",
                    points: [Point { x: 0, y: 0 }, Point { x: 1, y: 1 }],
                };
                print(console, path.name + ": " + str(len(path.points)));
                print(console, str(path.points[1].y));
            }
        """)
        self.assertEqual(out, "diag: 2\n1\n")

    def test_record_equality(self):
        out = run("""
            record Point {
                x: Int,
                y: Int,
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let a: Point = Point { x: 1, y: 2 };
                let b: Point = Point { x: 1, y: 2 };
                print(console, str(a == b) + " " + str(a != Point { x: 0, y: 0 }));
            }
        """)
        self.assertEqual(out, "true true\n")

    def test_recursion_through_list(self):
        out = run("""
            record Tree {
                value: Int,
                children: List[Tree],
            }
            fn total(t: Tree) -> Int {
                var sum: Int = t.value;
                var i: Int = 0;
                while i < len(t.children) {
                    sum = sum + total(t.children[i]);
                    i = i + 1;
                }
                return sum;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let t: Tree = Tree {
                    value: 1,
                    children: [
                        Tree { value: 2, children: [] },
                        Tree { value: 3, children: [Tree { value: 4, children: [] }] },
                    ],
                };
                print(console, str(total(t)));
            }
        """)
        self.assertEqual(out, "10\n")

    def test_capability_inside_record_works(self):
        # Bundling a capability in a record is normal object-capability style.
        out = run("""
            record Logger {
                out: Console,
                prefix: Text,
            }
            fn log(l: Logger, msg: Text) -> Unit ! {io.write} {
                print(l.out, l.prefix + msg);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let logger: Logger = Logger { out: console, prefix: "[app] " };
                log(logger, "started");
            }
        """)
        self.assertEqual(out, "[app] started\n")


class TestRecordRejection(unittest.TestCase):
    def test_unknown_field(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Point { x: Int, y: Int }
                fn main(c: Console) -> Unit {
                    let p: Point = Point { x: 1, y: 2 };
                    let z: Int = p.z;
                }
            """)
        self.assertIn("no field 'z'", ctx.exception.message)

    def test_fields_must_be_in_declaration_order(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Point { x: Int, y: Int }
                fn main(c: Console) -> Unit {
                    let p: Point = Point { y: 2, x: 1 };
                }
            """)
        self.assertIn("declaration order", ctx.exception.message)

    def test_missing_field(self):
        with self.assertRaises(CheckError):
            check_only("""
                record Point { x: Int, y: Int }
                fn main(c: Console) -> Unit {
                    let p: Point = Point { x: 1 };
                }
            """)

    def test_field_type_mismatch(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Point { x: Int, y: Int }
                fn main(c: Console) -> Unit {
                    let p: Point = Point { x: 1, y: "two" };
                }
            """)
        self.assertIn("needs Int", ctx.exception.message)

    def test_unknown_record_type(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn main(c: Console) -> Unit {
                    let p: Pointt = Pointt { x: 1 };
                }
            """)
        self.assertIn("unknown record", ctx.exception.message)

    def test_direct_recursion_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Loop { next: Loop }
                fn main(c: Console) -> Unit {
                }
            """)
        self.assertIn("infinite", ctx.exception.message)

    def test_mutual_recursion_rejected(self):
        with self.assertRaises(CheckError):
            check_only("""
                record A { b: B }
                record B { a: A }
                fn main(c: Console) -> Unit {
                }
            """)

    def test_record_with_capability_cannot_be_compared(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                record Logger { out: Console }
                fn main(console: Console) -> Unit {
                    let a: Logger = Logger { out: console };
                    let same: Bool = a == a;
                }
            """)
        self.assertIn("capability", ctx.exception.message)

    def test_list_of_capabilities_cannot_be_compared(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn main(fs: Fs) -> Unit {
                    let same: Bool = [fs] == [fs];
                }
            """)
        self.assertIn("capability", ctx.exception.message)

    def test_lowercase_record_name_rejected(self):
        with self.assertRaises((CheckError, ParseError)):
            check_only("""
                record point { x: Int }
                fn main(c: Console) -> Unit {
                }
            """)

    def test_uppercase_binding_rejected(self):
        with self.assertRaises(CheckError) as ctx:
            check_only("""
                fn main(c: Console) -> Unit {
                    let Total: Int = 1;
                }
            """)
        self.assertIn("lowercase", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
