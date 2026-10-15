# Bundled fonts — license notice

The viewer bundles these three typefaces locally so it renders correctly
**offline**, with no CDN or network dependency. All three are licensed under the
**SIL Open Font License, Version 1.1** (OFL-1.1), which permits bundling and
redistribution, including inside a distributed application, provided this notice
travels with the font files.

| File | Family | Source project | License |
|------|--------|----------------|---------|
| `space-grotesk.woff2` | Space Grotesk | Florian Karsten / Google Fonts | OFL-1.1 |
| `montserrat.woff2` | Montserrat | Julieta Ulanovsky / Google Fonts | OFL-1.1 |
| `ibm-plex-mono.woff2` | IBM Plex Mono | IBM / Google Fonts | OFL-1.1 |

Each file is the **Latin subset** in `woff2` format, fetched from the Google
Fonts `css2` endpoint. The full OFL-1.1 text is at
<https://openfontlicense.org/> and accompanies each upstream project.

These are display/UI assets only — they carry no tracking, no script, and no
runtime behaviour. If you replace or drop them, the app falls back to the
system sans/mono stack defined in `styles.css` and still works.
