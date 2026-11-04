# YouTube AI Agent Pro — User Guide (Mac, Apple Silicon)

## Install (once)

1. Download **YouTube-AI-Agent-Pro-macOS-arm64.dmg** from the latest release page.
2. Open it and drag **YouTube AI Agent Pro** into **Applications**.
3. Open the app from Launchpad or Applications.

   The first time only, macOS may block it. If you see "can't be opened because it is from an unidentified
   developer", **right-click the app → Open → Open**. On newer macOS versions, go to **System Settings →
   Privacy & Security → Open Anyway** instead. This happens because the app isn't signed with a paid Apple
   Developer ID. Later updates install from inside the app and don't ask again.

You don't need to install Python, Node or anything else, and you never need the Terminal.

## First launch

1. **Connect YouTube.**
   1. Type your channel's email address and click **Continue with Google**. The email only pre-selects
      that account on Google's sign-in page; it doesn't give the app any access.
   2. Approve on Google's page in your browser. If your account has several channels, pick one.
   3. The first time, you'll also create a free Google "Desktop app" sign-in ID. The wizard shows each click.
2. **Set up this Mac.**
   * The app reads your chip, unified memory, disk space and Metal support, then recommends models.
     With 8 GB of memory you get the light writer model plus Stable Diffusion 1.5.
   * Each download shows its size and licence and waits for your **Approve**.
   * Each file is checked against the verified checksum recorded in this release.
   * For the Tamil voice, accept the Indic Parler-TTS terms on Hugging Face, paste a free read token, listen
     to a sample, and approve it.
3. **Video preferences.** Choose the language, category, topic, formats and schedule.
4. **Automation.** Switch on **ENABLE AUTONOMOUS PUBLISHING** if you want it.
   * Uploads stay **private** until one private test upload succeeds and you authorize public publishing in
     Settings.

## Everyday

**Menu bar icon:**
* Show Dashboard
* Pause Agent / Resume Agent / Stop Agent
* View Jobs
* Quit Application

Closing the window keeps the work going. Quitting stops everything.

**System Health:**
* Choose **ECO**, **BALANCED** (default) or **HIGH QUALITY**.
* The app pauses new work on low battery, when the Mac is hot, or under memory pressure, and resumes by itself.
* While a video is being made on the power adapter, it can keep the Mac from idle-sleeping.
* Nothing runs while the Mac is asleep or shut down. Closing the lid sleeps the Mac.

On a MacBook Air M1, a Short takes very roughly 20–60 minutes and a 6-minute video several hours.

**MCP Connections:**
* Built-in read-only tools (media inspection, story library).
* You can add remote MCP servers. Each tool stays off until you approve it, and every call is logged.

## Updates

* The app checks at start-up and every 6 hours.
* When a new version exists, **Update Available** shows the current and new version, release notes and
  download size.
* **UPDATE NOW** does the rest in order:
  1. Lets running jobs finish their current step.
  2. Backs up the database.
  3. Downloads the update and verifies its signature.
  4. Installs it over the old app.
  5. Restarts and upgrades the database.
  6. Resumes your jobs.
* **REMIND ME LATER** asks again tomorrow.
* You never uninstall anything.

**Your data is never inside the app:**
* Settings, database and backups: `~/Library/Application Support/com.youtubeaiagentpro.app`
* Keys and tokens: the macOS Keychain (items named `com.youtubeaiagentpro.app`)
* Videos and images: the media folder, which you can move in System Health → Storage

**If something goes wrong:**
* A failed update leaves the current version running.
* Database backups are in **Updates → Backups**. They can be restored at the next start, including from the
  start-up error screen.

## Costs

| | In FREE ONLY mode |
|---|---|
| Paid AI APIs | ₹0 — they can't be called at all |
| Your Mac | Electricity |
| Internet | One-time model downloads (about 5–15 GB) and video uploads |
| Release hosting | Free (GitHub Releases) |

Optional paid providers (Claude, Gemini, Google TTS, Runway) are under **Settings → Premium providers**:
* Each needs explicit authorization, per-provider switches, and ₹ limits.
* The app never buys subscriptions.

Free or paid, no tool can guarantee YouTube monetization.
