"""Read section sizes from a stored ELF file. Nothing is executed.

text_bytes() is the Berkeley "text" figure that GNU `size` prints: the total
size of allocated, read-only sections that occupy file space (code, rodata,
vector tables, exception tables). Writable sections are "data" and NOBITS
sections are "bss", so neither counts.

The file is untrusted. Only the ELF header and the section header table are
read, and every offset is checked against the file's size.
"""
import os
import struct
from pathlib import Path

ELF_MAGIC = b"\x7fELF"
SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHT_NOBITS = 8
# Past this many sections the table alone would be over 64 MiB.
MAX_SECTIONS = 1 << 20

# Fields after e_ident, up to e_shstrndx; then one section header.
HEADER = {1: "HHIIIIIHHHHHH", 2: "HHIQQQIHHHHHH"}
SECTION = {1: "IIIIIIIIII", 2: "IIQQQQIIQQ"}


class ElfError(ValueError):
    pass


def text_bytes(path: Path) -> int:
    with path.open("rb") as f:
        file_size = os.fstat(f.fileno()).st_size
        ident = f.read(16)
        if len(ident) < 16 or ident[:4] != ELF_MAGIC:
            raise ElfError("not an ELF file")
        elf_class, encoding = ident[4], ident[5]
        if elf_class not in HEADER or encoding not in (1, 2):
            raise ElfError("unknown ELF class or byte order")
        order = "<" if encoding == 1 else ">"
        header_fmt, section_fmt = order + HEADER[elf_class], order + SECTION[elf_class]

        header = f.read(struct.calcsize(header_fmt))
        if len(header) < struct.calcsize(header_fmt):
            raise ElfError("truncated ELF header")
        fields = struct.unpack(header_fmt, header)
        shoff, shentsize, shnum = fields[5], fields[10], fields[11]
        if shoff == 0:
            raise ElfError("no section header table")
        if shentsize < struct.calcsize(section_fmt):
            raise ElfError("section headers are too small")

        def read_table(count: int) -> bytes:
            if shoff + count * shentsize > file_size:
                raise ElfError("section header table extends past the end of the file")
            f.seek(shoff)
            return f.read(count * shentsize)

        if shnum == 0:
            # Extended numbering: the real count is section 0's sh_size.
            shnum = struct.unpack_from(section_fmt, read_table(1))[5]
            if not 0 < shnum <= MAX_SECTIONS:
                raise ElfError("implausible section count")
        table = read_table(shnum)

    total = 0
    for i in range(shnum):
        _, sh_type, flags, _, offset, size, *_ = struct.unpack_from(section_fmt, table, i * shentsize)
        if flags & SHF_ALLOC and not flags & SHF_WRITE and sh_type != SHT_NOBITS:
            if offset + size > file_size:
                raise ElfError(f"section {i} extends past the end of the file")
            total += size
    return total
