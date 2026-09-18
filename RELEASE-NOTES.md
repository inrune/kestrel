# Kestrel v1.4.8

**Fixed**

- A received payment stayed "on its way" until you restarted the app.
- Pending transactions never expired, so a dropped one sat there forever.
- The wallet could show a balance it would then refuse to spend.
- A losing double-spend was reported as confirmed.
- After syncing from a peer, a restart could duplicate the chain file and wipe the mempool.
- A failed disk write during a reorg could splice two chains together.
- One slow client could freeze the whole node.
- A peer-book race could silently kill background maintenance for the rest of the run.
- One exception could freeze the whole window.
- Notifications floated outside the app and drifted when you moved or switched windows. They are part of the window now.
- The miner's wallet tab counted your own change as money received.
- A screenful of Python could appear over your terminal.

**New**

- In-app updates. The app notices a new release, shows what changed, and installs it if you say yes. It never touches your wallet, address book, settings or chain data.
- Payments show how long they have been waiting.
- A screen that has stopped updating now says so instead of showing stale numbers.
- The launcher offers to install Python for you if it is missing.

**Faster**

- The chain is no longer rewritten from scratch on every block.
- Opening the app no longer re-verifies the entire chain.
- One bad block no longer costs you the whole chain.
- A disk that won't take a write no longer stops a node.
- Dragging and resizing the window is much lighter.

**Also**

- Both apps redesigned.
- New website.
- 134 tests, all running.

---

Anyone on v1.4.7 has to update by hand this once — the updater ships *in* this version. After this it is automatic.
