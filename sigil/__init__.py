"""Sigil — a capability-secure, effect-typed, contract-carrying language
designed for AI authorship and human audit.

This Python package is the bootstrap (reference) toolchain. Sigil reached 1.0
when it became self-hosting: cc0, a Sigil compiler written in Sigil
(selfhost/cc0.sg), compiles its own source into a static x86-64 Linux ELF that
recompiles cc0's source to a byte-identical copy of itself — with no Python,
rustc, LLVM, assembler, linker, or libc on the compile path. The Python
toolchain remains the reference semantics and the bootstrap compiler."""

__version__ = "1.0.0"
