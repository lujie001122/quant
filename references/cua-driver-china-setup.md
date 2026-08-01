# cua-driver Installation on China-Mainland macOS

GitHub is blocked on China-mainland networks. Use the `ghfast.top` mirror to download both the install script and the release tarball.

## Steps

1. Download install script via mirror:
   ```bash
   curl -sL "https://ghfast.top/https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh" -o /tmp/cua_install.sh
   ```

2. Check the baked release version in the script, then download the tarball:
   ```bash
   # Example: version 0.14.1, darwin-universal
   curl -L -o /tmp/cua-driver.tar.gz \
     "https://ghfast.top/https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.14.1/cua-driver-rs-0.14.1-darwin-universal.tar.gz"
   ```

3. Extract and install manually:
   ```bash
   cd /tmp && tar -xzf cua-driver.tar.gz
   mkdir -p ~/.local/bin
   cp /tmp/cua-driver-rs-*/cua-driver ~/.local/bin/
   cp /tmp/cua-driver-rs-*/cua-cursor-theme ~/.local/bin/
   chmod +x ~/.local/bin/cua-driver ~/.local/bin/cua-cursor-theme
   ```

4. Install CuaDriver.app:
   ```bash
   cp -r /tmp/cua-driver-rs-*/CuaDriver.app /Applications/
   ```

5. Add to PATH (if not already):
   ```bash
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   ```

6. Verify: `cua-driver --version`

7. Grant macOS permissions:
   - System Settings → Privacy & Security → Accessibility → enable CuaDriver
   - System Settings → Privacy & Security → Screen Recording → enable CuaDriver

8. Confirm: `hermes computer-use doctor`

9. **Also grant Accessibility to Terminal.app** (or whatever terminal Hermes runs in): System Settings → Privacy & Security → Accessibility → enable Terminal. This is separate from CuaDriver's grant. Without it, AppleScript `System Events` calls (used by Evolving and direct `osascript` commands) will hang silently — no error, just timeout.

## Notes

- `cua.ai` (the redirect domain) is accessible from China — only `github.com` and `githubusercontent.com` are blocked.
- The install script itself tries to download from GitHub Releases, which fails. That's why we download the tarball separately via the mirror.
- The `ghfast.top` mirror works for both raw files and release assets. If it goes down, try `github.moeyy.xyz` or `gh-proxy.com`.
