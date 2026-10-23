# Validation — 23 October 2026

Environment: Windows, Python 3.11.15, PySide6 6.12.0, Requests 2.34.2, Beautiful Soup 4.15.0, pytest 9.1.1.

- 14 offline automated tests passed, including asynchronous UI success/error states.
- Actual Qt window rendered and inspected at 920×760 with English labels and the dark purple theme.
- Live CDX lookup for example.com returned captures; downloading the first HTML capture returned 6814 bytes.
- A small live recovery saved HTML and reported an archived missing image as HTTP 404 with `partial` status, preserving output and report.
- User verified recovery of kun.uz for requested timestamp 20180102065147: 94 files saved, 6 failed, file limit 100 reached. The local page opened with text, styles and some images; some images remained missing.
- Repeated recovery runs create unique subfolders and preserve access to earlier results.

The screenshot in docs shows the actual application interface. No recovered website contents, personal local paths or raw user logs are distributed.

Not validated: every archive format, every site, exhaustive browser behavior, standalone EXE packaging or full-site completeness. The initial window-close report was not conclusively reproduced; worker lifetime handling and error logging were strengthened, and asynchronous UI success/failure tests pass.
