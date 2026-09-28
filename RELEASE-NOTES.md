# Kestrel v1.4.9

**Fixed**

- Switching to a heavier chain froze the node for half a minute or more. Now it only checks the blocks after the fork, and a reorg no longer rewrites the whole ledger file.
- The in-app updater could delete your own files, including a backup key — and if it couldn't read your wallet, it replaced it instead of setting it aside. Both fixed; this version also restores anything the old updater removed.
- Sending: a timed-out payment could show as failed when it had gone through, and retrying paid twice. Fixed, with a warning before any duplicate payment.
- Windows launchers (`run.bat`, `start.bat`) could fail from wrong line endings; run.sh lost its permissions after an update on Mac/Linux; "externally managed" Pythons (Debian 12+, Ubuntu 23.04+, Homebrew) failed to launch at all. All fixed.
- Smaller fixes: double-mining from a fast Stop/Start, wallet history capped at 13 hours, a damaged mempool file blocking startup, one bad Wi-Fi packet disabling LAN discovery, a failed update leaving the app closed, a slow peer stalling sync for everyone.

**New**

- Optional beta versions: *Settings → Get beta versions too*. Off by default.
- Downloads are checked against GitHub's own SHA-256 for each file; the release zips are reproducible from the tag.
- Pending payments are re-sent every ten minutes so one from behind a router can't get stranded.
- Pages scroll properly on small windows. `/health` for monitoring; peer versions shown in the Network tab.

**Faster**

- The node stays responsive while catching up or switching forks. Balances, the rich list and the explorer index no longer rescan the whole chain.

214 tests, run on Python 3.10 to 3.13.

---

Updating from 1.4.8 in the app works as before. The first start of 1.4.9 repairs anything the 1.4.8 updater got wrong, and tells you if it put files back.
