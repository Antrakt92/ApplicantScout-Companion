"""Apply the Windows adjustments to the pinned ZBar source."""
from pathlib import Path


SOURCE = Path("/tmp/zbar")
EDITS = {
    "zbar/decoder.c": (
        ("#include <stdio.h>", "#include <stddef.h> /* offsetof */\n#include <stdio.h>"),
        ("(long)&dcode->buf_alloc - (long)dcode", "offsetof(zbar_decoder_t, buf_alloc)"),
    ),
    "zbar/processor/win.c": (
        ("(LPCTSTR)(long)wca", "MAKEINTATOM(wca)"),
        ("(LPCTSTR)(long)proc->state->registeredClass", "MAKEINTATOM(proc->state->registeredClass)"),
    ),
    "zbar/qrcode/qrdectxt.c": (
        (
            "\t\t    char *in;",
            "#ifdef HAVE_ICONV_CONST_INPUT\n\t\t    const char *in;\n#else\n\t\t    char *in;\n#endif",
        ),
    ),
}


def main() -> None:
    for name, edits in EDITS.items():
        path = SOURCE / name
        contents = path.read_text(encoding="utf-8")
        for old, new in edits:
            if contents.count(old) != 1:
                raise ValueError(f"Unexpected ZBar source at {name}")
            contents = contents.replace(old, new)
        path.write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    main()
