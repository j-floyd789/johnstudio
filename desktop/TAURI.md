# Wrapping the JohnStudio UI in Tauri (next step)

The code under `desktop/` is structured to be wrapped by Tauri without changes.
Tauri itself is not committed to this repo because the current environment does
not have a Rust toolchain.

When you're ready:

```bash
# 1. Install Rust (one-time).
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
# Restart shell, then:
rustc --version  # should print 1.7x+

# 2. Add Tauri to this directory.
cd desktop
npm create tauri-app@latest -- --template react-ts --manager npm --identifier com.johnstudio.app

# When prompted:
#   - "where should we initialize" → answer "." (this folder)
#   - "frontend dev command"       → npm run dev
#   - "frontend build command"     → npm run build
#   - "frontend dist directory"    → ../desktop/dist  (relative to src-tauri/)

# 3. Tauri config tweaks (src-tauri/tauri.conf.json):
#    "build": { "devUrl": "http://localhost:5173", "frontendDist": "../dist" }
#    "app":   { "windows": [ { "title": "JohnStudio", "width": 1280, "height": 800 } ] }
#    "app.security.csp" should permit http://127.0.0.1:8765 connect-src.

# 4. Run in dev:
npm run tauri dev

# 5. Build:
npm run tauri build
```

## Important integration notes

- The UI talks to the JohnStudio backend at `http://127.0.0.1:8765` via plain
  `fetch`. Tauri does not need to proxy this — both the dev shell and the
  packaged app load the same `client.ts` and target the same local host.
- The backend is launched separately (`johnstudio server`). Optionally, you can
  use Tauri's sidecar feature to launch the Python backend from the bundled app.
- CORS in `johnstudio/server.py` already whitelists `tauri://localhost`.
- The packaged app should still require the user to have Python + the
  `johnstudio` CLI installed. Bundling Python inside a Tauri app is out of MVP
  scope.
