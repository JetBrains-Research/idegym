# xterm.js

The dashboard's pod shell renders its terminal with [xterm.js](https://github.com/xtermjs/xterm.js).
The files are copied unmodified from the npm packages below, so the dashboard needs no build step and
no CDN (it is usually reached over a VPN). Both packages are MIT-licensed; their licenses sit next to
the files.

| File | Package | Path in the package |
|------|---------|---------------------|
| `xterm.js` | `@xterm/xterm@6.0.0` | `lib/xterm.js` |
| `xterm.css` | `@xterm/xterm@6.0.0` | `css/xterm.css` |
| `LICENSE` | `@xterm/xterm@6.0.0` | `LICENSE` |
| `addon-fit.js` | `@xterm/addon-fit@0.11.0` | `lib/addon-fit.js` |
| `LICENSE.addon-fit` | `@xterm/addon-fit@0.11.0` | `LICENSE` |

Tarball integrity, as published on the npm registry:

- `@xterm/xterm@6.0.0`: `sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==`
- `@xterm/addon-fit@0.11.0`: `sha512-jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19VwQei89QCiZZP86DCPF7g==`

To upgrade, download the tarballs from `https://registry.npmjs.org/@xterm/xterm/-/xterm-<version>.tgz`
and `https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-<version>.tgz`, check them against the
`dist.integrity` the registry reports, copy the same paths over, and update this table. The source
maps are left out on purpose, so browser developer tools report the `.map` files as missing.
