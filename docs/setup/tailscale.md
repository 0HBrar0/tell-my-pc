# Tailscale: reach your computer from your phone, privately

The daemon only listens on your computer's Tailscale address. Nothing is exposed to the
internet or your local network.

## 1. Computer

1. Install Tailscale: <https://tailscale.com/download>. Sign in (Google, Microsoft, GitHub or
   Apple account). This creates your private network ("tailnet").
2. Check it is up:

   ```bash
   tailscale status
   tailscale ip -4
   ```

   The IP starts with `100.`. Note the machine name in the first column of `tailscale status`.
3. Optional but recommended: in the admin console (<https://login.tailscale.com/admin/dns>)
   turn on **MagicDNS**, so the phone can use `http://<machine>.<tailnet>.ts.net:8787`. The full
   name is shown under **Machines** → your computer.
4. In your tell-my-pc config keep `server.bind: tailscale`. Run `tell-my-pc check`; it prints
   the address it will listen on.

Windows firewall: the first `tell-my-pc serve` may show a firewall prompt. Allow it on
**Private networks** only. Tailscale's interface counts as private.

## 2. iPhone

1. Install **Tailscale** from the App Store and sign in with the **same** account.
2. Turn it on once and check your computer shows as connected.

## 3. Keep the phone connected when Siri needs it

The Shortcut reaches your computer through Tailscale, so the iPhone must be connected to
Tailscale at that moment. There is **no domain field to fill in**: Tailscale recognises
`*.ts.net` addresses by itself. Pick one option.

**Easiest: leave Tailscale on.** Open the Tailscale app and turn the main switch on
(**Connected**). While it is on, the app keeps the connection up by itself, across restarts
and Wi-Fi/mobile changes.

**Optional: connect only when needed** (Tailscale for iOS 1.48 or newer):

1. In the Tailscale app, tap your **profile picture** (top right).
2. Tap **VPN On Demand** and turn it on. (Missing? Update the app from the App Store.)
3. Set **Wi-Fi** to **Do Nothing** and **Cellular** to **Do Nothing**.

With every rule on **Do Nothing**, iOS brings Tailscale up whenever something, such as the
Shortcut, opens a `*.ts.net` address ("MagicDNS hostname matching" in
[Tailscale's docs](https://tailscale.com/kb/1291/ios-vpn-on-demand)). Any other rule turns
that matching off, and **Never** stops Tailscale from connecting at all.

## 4. Test

From the phone's Safari, open `http://<machine>.<tailnet>.ts.net:8787/health`. You should
see `{"ok":true}`. Every other path needs the token, so it will say "Not authorized." in a
browser, which is expected.
