"""End-to-end tests for the Sigil v0.1 prototype.

The most important tests here are the REJECTION tests: Sigil's value is the
programs it refuses to run.
"""

import io
import unittest

from sigil.checker import check
from sigil.errors import CheckError, ContractViolation, RuntimeFault
from sigil.interp import Interpreter
from sigil.parser import parse


def run(source: str, stdin: str = "") -> str:
    program = parse(source)
    sigs = check(program)
    out = io.StringIO()
    Interpreter(program, sigs, stdin=io.StringIO(stdin), stdout=out).run_main()
    return out.getvalue()


def check_only(source: str) -> None:
    check(parse(source))


class TestExecution(unittest.TestCase):
    def test_hello(self):
        out = run("""
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, "hello, " + "world");
            }
        """)
        self.assertEqual(out, "hello, world\n")

    def test_while_and_vars(self):
        out = run("""
            fn fib(n: Int) -> Int
                requires n >= 0
                ensures result >= 0
            {
                var a: Int = 0;
                var b: Int = 1;
                var i: Int = 0;
                while i < n {
                    let next: Int = a + b;
                    a = b;
                    b = next;
                    i = i + 1;
                }
                return a;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(fib(20)));
            }
        """)
        self.assertEqual(out, "6765\n")

    def test_lists(self):
        out = run("""
            fn total(xs: List[Int]) -> Int {
                var sum: Int = 0;
                var i: Int = 0;
                while i < len(xs) {
                    sum = sum + xs[i];
                    i = i + 1;
                }
                return sum;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let xs: List[Int] = push([3, 1, 4], 1);
                print(console, str(total(xs)) + " from " + str(len(xs)));
            }
        """)
        self.assertEqual(out, "9 from 4\n")

    def test_read_line(self):
        out = run("""
            fn main(console: Console) -> Unit ! {io.read, io.write} {
                let name: Text = read_line(console);
                print(console, "hi " + name);
            }
        """, stdin="ada\n")
        self.assertEqual(out, "hi ada\n")

    def test_if_else_and_truncating_division(self):
        out = run("""
            fn classify(n: Int) -> Text {
                if n < 0 {
                    return "neg";
                } else if n == 0 {
                    return "zero";
                } else {
                    return "pos";
                }
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, classify(0 - 7));
                print(console, str((0 - 7) / 2));
            }
        """)
        self.assertEqual(out, "neg\n-3\n")


class TestEffectRejection(unittest.TestCase):
    """A function that does not declare an effect cannot perform it."""

    def test_pure_function_cannot_print(self):
        src = """
            fn sneaky(c: Console, x: Text) -> Int {
                print(c, x);
                return 1;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let n: Int = sneaky(console, "data");
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("io.write", ctx.exception.message)
        self.assertIn("pure", ctx.exception.message)

    def test_effects_propagate_transitively(self):
        src = """
            fn logger(c: Console, msg: Text) -> Unit ! {io.write} {
                print(c, msg);
            }
            fn quiet(c: Console) -> Unit {
                logger(c, "leak");
            }
            fn main(console: Console) -> Unit ! {io.write} {
                quiet(console);
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("io.write", ctx.exception.message)

    def test_fs_effect_separate_from_io(self):
        src = """
            fn save(fs: Fs, data: Text) -> Unit ! {io.write} {
                write_file(fs, "x.txt", data);
            }
            fn main(console: Console, fs: Fs) -> Unit ! {io.write, fs.write} {
                save(fs, "d");
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("fs.write", ctx.exception.message)

    def test_unknown_effect_rejected(self):
        src = """
            fn f() -> Unit ! {net.send} {
            }
            fn main(console: Console) -> Unit {
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("unknown effect", ctx.exception.message)


class TestCapabilityRejection(unittest.TestCase):
    """No ambient authority: effects also need a capability in hand."""

    def test_cannot_print_without_console(self):
        src = """
            fn helper(data: Text) -> Unit ! {io.write} {
                print(data);
            }
            fn main(console: Console) -> Unit ! {io.write} {
                helper("secrets");
            }
        """
        with self.assertRaises(CheckError):
            check_only(src)

    def test_cannot_pass_wrong_capability(self):
        src = """
            fn main(console: Console, fs: Fs) -> Unit ! {io.write} {
                print(fs, "hi");
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("Console", ctx.exception.message)

    def test_capabilities_cannot_be_compared(self):
        src = """
            fn main(console: Console, fs: Fs) -> Unit {
                let same: Bool = console == console;
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("capability", ctx.exception.message)

    def test_contract_clauses_must_be_pure(self):
        src = """
            fn shout(c: Console, msg: Text) -> Unit ! {io.write} {
                print(c, msg);
            }
            fn f(c: Console, x: Int) -> Int ! {io.write}
                requires x > 0 or noisy(c)
            {
                return x;
            }
            fn noisy(c: Console) -> Bool ! {io.write} {
                shout(c, "side effect in a contract!");
                return true;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                let n: Int = f(console, 1);
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("pure", ctx.exception.message)


class TestContracts(unittest.TestCase):
    def test_requires_blames_caller(self):
        src = """
            fn safe_div(a: Int, b: Int) -> Int
                requires b != 0
            {
                return a / b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(safe_div(1, 0)));
            }
        """
        with self.assertRaises(ContractViolation) as ctx:
            run(src)
        self.assertEqual(ctx.exception.blame, "caller")
        self.assertIn("b != 0", ctx.exception.message)

    def test_ensures_blames_callee(self):
        src = """
            fn abs_broken(n: Int) -> Int
                ensures result >= 0
            {
                return n;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(abs_broken(0 - 5)));
            }
        """
        with self.assertRaises(ContractViolation) as ctx:
            run(src)
        self.assertEqual(ctx.exception.blame, "callee")
        self.assertIn("abs_broken", ctx.exception.message)

    def test_satisfied_contracts_pass_silently(self):
        out = run("""
            fn safe_div(a: Int, b: Int) -> Int
                requires b != 0
                ensures result * b <= a
            {
                return a / b;
            }
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(safe_div(10, 3)));
            }
        """)
        self.assertEqual(out, "3\n")


class TestTypeRejection(unittest.TestCase):
    def test_no_shadowing(self):
        src = """
            fn f(x: Int) -> Int {
                let x: Int = 2;
                return x;
            }
            fn main(console: Console) -> Unit {
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("shadow", ctx.exception.message)

    def test_let_is_immutable(self):
        src = """
            fn main(console: Console) -> Unit {
                let x: Int = 1;
                x = 2;
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("immutable", ctx.exception.message)

    def test_type_mismatch(self):
        src = """
            fn main(console: Console) -> Unit {
                let x: Int = "hello";
            }
        """
        with self.assertRaises(CheckError):
            check_only(src)

    def test_missing_return_path(self):
        src = """
            fn f(n: Int) -> Int {
                if n > 0 {
                    return 1;
                }
            }
            fn main(console: Console) -> Unit {
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("return", ctx.exception.message)

    def test_no_text_plus_int(self):
        src = """
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, "n = " + 3);
            }
        """
        with self.assertRaises(CheckError) as ctx:
            check_only(src)
        self.assertIn("str(", ctx.exception.message)


class TestRuntimeFaults(unittest.TestCase):
    def test_division_by_zero(self):
        src = """
            fn main(console: Console) -> Unit ! {io.write} {
                print(console, str(1 / 0));
            }
        """
        with self.assertRaises(RuntimeFault):
            run(src)

    def test_index_out_of_range(self):
        src = """
            fn main(console: Console) -> Unit ! {io.write} {
                let xs: List[Int] = [1, 2, 3];
                print(console, str(xs[3]));
            }
        """
        with self.assertRaises(RuntimeFault):
            run(src)


if __name__ == "__main__":
    unittest.main()
