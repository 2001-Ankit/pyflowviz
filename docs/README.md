# Documentation

`documentation.html` is the source — edit that. It is a single self-contained
file with print styles, so it reads fine in a browser and paginates correctly
as a PDF.

The generated PDF is not committed (see `.gitignore`). Regenerate it with any
Chromium-based browser:

**Windows**

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --headless=new --disable-gpu --no-pdf-header-footer `
  --print-to-pdf="docs\pyflowviz-documentation.pdf" `
  "file:///d:/python-visualizer/docs/documentation.html"
```

**macOS / Linux**

```bash
chromium --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=docs/pyflowviz-documentation.pdf \
  "file://$(pwd)/docs/documentation.html"
```

Or just open the HTML in a browser and print to PDF.

If you want the PDF available to other people, attach it to a GitHub Release
rather than committing it — that keeps generated binaries out of the history
while still giving people a download link.
