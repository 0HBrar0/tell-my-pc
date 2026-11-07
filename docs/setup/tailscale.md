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

## 3. VPN On Demand for `*.ts.net`

So Siri works without you opening the Tailscale app first:

1. iPhone **Settings** → **VPN** (or **General** → **VPN & Device Management** → **VPN**).
2. Tap the **(i)** next to **Tailscale**.
3. Turn on **Connect On Demand**.
4. In the Tailscale app: **Settings** → **VPN On Demand** (iOS app 1.50 or later). Add a rule:
   - **Domain**: `ts.net` (matches every `*.ts.net` name), action **Connect**.
   - Leave other networks on the default.

Now any request to `*.ts.net`, including the Shortcut, brings the tunnel up by itself.

## 4. Test

From the phone's Safari, open `http://<machine>.<tailnet>.ts.net:8787/health`. You should
see `{"ok":true}`. Every other path needs the token, so it will say "Not authorized." in a
browser, which is expected.
