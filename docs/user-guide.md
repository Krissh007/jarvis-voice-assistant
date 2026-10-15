# User guide

- [Setup](#setup)
- [Starting and stopping JARVIS](#starting-and-stopping-jarvis)
- [Talking to JARVIS](#talking-to-jarvis)
- [Confirming actions](#confirming-actions)
- [The window, tray and taskbar](#the-window-tray-and-taskbar)
- [What JARVIS can do](#what-jarvis-can-do)
- [The wake word](#the-wake-word)
- [Troubleshooting](#troubleshooting)

## Setup

You need Windows 10 or 11, **Google Chrome**, and about 2 GB of free disk space.

1. **Install uv**, the Python package manager. It also installs Python 3.12 for you.
   ```bash
   pip install --user uv
   ```
   No Python at all? Use the installer from <https://docs.astral.sh/uv/> instead.
2. **Get the code**, then open a terminal in the `jarvis` folder.
3. **Install the dependencies**, including the offline wake word:
   ```bash
   python -m uv sync --extra wakeword
   ```
4. **Add your API key.** Copy `.env.example` to `.env` and put your key after `GOOGLE_API_KEY=`
   (see [API keys](#api-keys)). `.env` is never committed or shared.
5. **Check everything works:**
   ```bash
   python -m uv run pytest
   ```
6. **Test the microphone and wake word:**
   ```bash
   .\run.bat --mic-test
   ```
   If JARVIS only wakes when you shout, [train it on your voice](#the-wake-word).
7. **Optional: sign JARVIS's browser in to YouTube Premium** (no ads, your recommendations). This
   opens JARVIS's *own* Chrome profile, never your everyday one:
   ```bash
   .\run.bat --browser-login
   ```
   Sign in, choose **your own** YouTube profile, say **No thanks** to sync and password saving,
   then close the window.
8. **Optional: a desktop icon** that starts JARVIS as an app, with no terminal:
   ```bash
   .\run.bat --install-shortcut
   ```
   **To pin it to the taskbar:** right-click the desktop icon > *Show more options* > *Pin to
   taskbar*. JARVIS's window then shares that button. Pin the desktop icon, not a running window.

### API keys

| Key | Needed? | Where | What for |
|---|---|---|---|
| `GOOGLE_API_KEY` | **Yes** | <https://aistudio.google.com/apikey> (**Create API key**) | Gemini Live voice and Gemini Flash text |
| `TAVILY_API_KEY` | Optional | <https://tavily.com> (free plan) | Better web search; DuckDuckGo is used without it |
| `OPENROUTER_API_KEY` | Optional | <https://openrouter.ai/keys> | A fallback text model when Gemini is busy; also set `models.openrouter` |

Free-tier limits are per account and change often; see yours at
<https://aistudio.google.com/rate-limit>. Some Flash models allow only about 20 requests a day. When one
runs out, JARVIS skips it for an hour and uses the next model in `models.flash_fallbacks`.

## Starting and stopping JARVIS

| Start it | What you get |
|---|---|
| **Desktop or taskbar icon** | The JARVIS window and tray icon, no terminal. Printed output goes to `logs/console.txt`. |
| `.\run.bat` | The same, plus a terminal that shows everything live. Ctrl+C quits. |
| `.\run.bat --text` | Type instead of talk, in the terminal (handy for testing). |
| `.\run.bat --dry-run` | Actions are logged instead of performed. |

- The window appears straight away showing **Starting up…**, and JARVIS is ready a few seconds later.
- **Only one JARVIS runs at a time.** Starting it again while it's running brings its window to the
  front. Relaunching right after Quit works straight away.
- **To quit:** the window's **X**, its right-click menu (*Quit JARVIS*), or the tray menu. With
  `window.close_button: hide`, the X hides the window to the tray instead.
- Startup problems, such as a missing key, appear in a pop-up.

## Talking to JARVIS

**Wake it:** say **"hey jarvis"**, click the orb or **Talk**, left-click the tray icon, or press
**Ctrl+Alt+Space**. A soft beep means it's listening. Wake it while it's already awake and it answers
"Yes?".

**Sleep:** say "go to sleep", click **Sleep**, or just stop talking: it sleeps after 15 seconds of
quiet. While asleep, nothing you say leaves your PC.

**Or type:** write in the box at the bottom of the window and press **Enter** (or click **Send**).
JARVIS answers in the conversation and, normally, out loud too. Turn off *Speak replies to typed
messages* in the right-click menu for silent, text-only answers, e.g. in a meeting. Typing never
turns the microphone on. **Up** and **Down** bring back earlier messages, and **Esc** clears the box.

| You say | What happens |
|---|---|
| "What's the weather in Hyderabad?" | Answers straight away |
| "Research the best budget headphones" | Searches, reads up to 3 pages, answers, offers to save notes |
| "Save that" | Saves the notes to `Documents\jarvis\` (asks first; never overwrites) |
| "Open Notepad" / "Close Notepad" | Opens straight away / asks first, then closes |
| "Open my resume" | Finds it in your folders and opens it; if several files match, asks which one |
| "What did I download today?" / "Find my photos from last week" | Lists the newest matches |
| "Show the second one in its folder" | Opens File Explorer with that file selected |
| "Play Believer by Imagine Dragons" | Plays that song in your YouTube Music app |
| "Play the Rolling Stones" | Plays that band's most popular song |
| "Pause" / "Next" / "What's playing?" | Controls YouTube Music |
| "Turn the music down to 20" | Changes only the music, not JARVIS's voice |
| "Close the music player" | Closes the YouTube Music app |
| "Open naukri.com" | Opens it in JARVIS's browser (asks first for sites not on your trusted list) |
| "What's on my screen?" | Looks once, with private windows blacked out |
| "Read me Claude's reply" / "What does Notepad say?" | Reads the text in that app (never browsers or password fields) |
| "What can I click here?" | Lists the buttons and fields in the app you're using |
| "Type 'see you at six' in Notepad" | Types it where the cursor is (your clipboard is put back) |
| "Ask Claude what a monad is" | Types the question into Claude, sends it, and can read you the answer |
| "Click New chat" / "Press Ctrl+N" / "Scroll down" | Acts in the app you're using |
| "Put Notepad on the left half" / "Minimise Spotify" | Moves the window |
| "Ask Claude to explain monads and read me the answer" | Sends it, waits for Claude to finish, reads the reply |
| "Open Notepad and write a shopping list: milk, eggs, bread" | Reads the plan, then does it after one "yes" |
| "Translate what I copied into Telugu" | Reads it aloud in Telugu |
| "Fix what I copied" | Rewrites it, reads a preview, replaces the clipboard after you confirm |
| "Research X, open its official page and tell me the price" | Reads a plan and waits for one "yes" |
| "Remember that my car is on level 3" | Saves it to memory |
| "What do you remember about my car?" / "Forget that" | Recalls it / deletes it (asks first) |
| "Remind me in 20 minutes to call Mom" | Speaks it and shows a notification when it's due |

**Music while you talk:** while JARVIS is awake, other apps drop to 30% of their volume and go back
up when it sleeps. JARVIS's own voice is never lowered.

**Listening modes** (`listening.mode`): `wake_word` (the default), `always_on` (the mic always
streams to Gemini), or `push_to_talk` (hold Ctrl+Alt+Space while you speak).

## Confirming actions

Anything that changes something (closing an app, saving a file, typing on a website, replacing
the clipboard, running a plan, forgetting a memory) is read to you first:

> "Close Notepad. It may ask to save. Should I go ahead?"

- **Yes:** say *yes, yeah, yep, sure, okay, confirm* or *go ahead* (a few filler words are fine:
  "yeah, go ahead"), or press **Ctrl+Alt+Y**.
- **No:** say anything else, press **Ctrl+Alt+N**, or stay silent for 20 seconds.
- Or **type** *yes* in the window's box.
- Your answer only counts once JARVIS has finished speaking, so its own voice can never confirm.
- "Yes, but change it to…" is a no: ask again with the change.

| Hotkey | Does |
|---|---|
| Ctrl+Alt+Space | Wake JARVIS (hold it to talk in `push_to_talk` mode) |
| Ctrl+Alt+M | Mute or unmute the microphone |
| Ctrl+Alt+S | Stop JARVIS talking, and cancel what it's doing |
| Ctrl+Alt+Y / Ctrl+Alt+N | Confirm / cancel the waiting action |

## The window, tray and taskbar

**The window** is a normal Windows window with a dark title bar, bottom-right at first:

- **The orb** spins and pulses with your voice or JARVIS's. Its colour shows the state: grey is
  asleep, blue is listening, cyan is speaking, red is muted. Click it to talk.
- **The conversation** below it shows everything you both said, scrollable, newest at the bottom.
- **The typing box** under the conversation: type to JARVIS instead of talking (Enter sends).
- **Buttons:**
  - **Talk** wakes JARVIS.
  - **Sleep** sends it to sleep.
  - **Mute** mutes the microphone.
  - **Stop** stops JARVIS talking (and anything it's doing); your music keeps playing. Press it
    again while JARVIS is quiet to pause the music.
- **Move, resize, snap and minimise** it like any window. Its size and position are remembered.
  Minimising sends it to its taskbar button.
- **Right-click** for:
  - *Talk* and *Go to sleep*
  - *Always on top*
  - *Speak replies to typed messages*
  - *Compact (orb only)*
  - *Minimise* and *Hide*
  - *Open log*
  - *Quit*

**The tray icon** (by the clock, perhaps under the **^** arrow) uses the same colours:

- **Left-click** to talk.
- **Right-click** for *Go to sleep*, *Stop*, *Mute*, *Show JARVIS window* and *Quit*.

## What JARVIS can do

Each capability can be switched off under `features:` in `config.yaml`.

**Apps.** JARVIS opens only the apps listed in `apps.allowlist` (plus their `aliases`), and never
guesses. Closing asks first and works like clicking X; say "close it without saving" to discard
changes. It can also switch to an open app.

**Your files.** JARVIS searches file and folder *names* in Desktop, Documents, Downloads, Pictures,
Music and Videos (following OneDrive), plus any `files.extra_folders`. It never reads what's inside a
file. It opens a file with its usual app only when it's clearly the one you meant; otherwise it lists
the matches and asks. Programs, scripts and shortcuts are never opened (it can show them in their
folder instead), and names that look private (passwords, keys, 2FA codes, ID cards, card details) are
never listed at all. New downloads show up at once; files deeper in folders within about 10 minutes.

**Web research.** It searches with Tavily or DuckDuckGo, reads up to 3 pages per question, and keeps
summaries for 24 hours. A site that refuses plain downloads gets one retry in a throwaway Chrome.
Local and private network addresses are never fetched. Weather comes from Open-Meteo, with no key.

**Browser.** JARVIS uses your installed Chrome with its own profile (`data/browser_profile`). Short
tasks run up to 10 steps: links and play buttons on the same site run straight away, while typing
(only on `sites.action_allowlist`), other clicks and leaving the site all ask first.

**Music.** It finds songs with ytmusicapi, preferring originals over covers, and plays them in the
installed YouTube Music app. Without the app, it plays them in JARVIS's browser.

**App control.** For apps without their own tool, JARVIS plans the exact UI Automation steps, reads
them to you, and runs them only after you confirm. It never reads text inside apps, never touches
password, account or settings controls, and never saves (Ctrl+S).

**Reading apps.** JARVIS can read the text, buttons and fields of an open app through Windows UI
Automation, without a screenshot. Name the app, or say "it" for the one you last asked about (or
the one in front). It won't read password managers, web browsers, Windows Settings or Security,
sign-in prompts, or windows whose title looks private, and it never reads password or payment
fields. A minimised app has nothing on screen to read.

**Typing in apps.** JARVIS types where the cursor is, or into a field you name. It asks first before
pressing Enter (except in AI chat apps like Claude), before typing into a terminal, and after it has
read text from an app or page in the same request. It never types into password or payment fields.

**Clicking and keys.** Name a button ("click New chat") or use a number JARVIS read out. If it can't
find the name, it asks "did you mean...?". Buttons such as Delete, Send, Save or Pay, and keys that save,
close or print, ask first. Account and sign-out buttons are never clicked, and Windows-key shortcuts
are never pressed. Windows can be minimised, maximised, restored, brought to the front, or snapped to
the left or right half of the screen.

**Several steps in apps.** For tasks such as opening an app, writing something and saving it, JARVIS
reads a numbered plan with the exact words it will type, then runs it after one "yes". Saving,
deleting and other risky steps still ask on their own. After sending a message to an AI chat app,
it waits until the reply stops changing (up to a minute) and reads only the new text.

**Screen and clipboard.** The screen is captured once per request, kept in memory only, with private
windows blacked out. Copied text that looks like a secret is never sent anywhere.

**Plans.** A planner drafts steps using only real tools. You approve the plan once, and then it's
frozen: web pages can't change it, and each step still follows its own safety rules.

**Memory** (`data/memory.json`). JARVIS keeps up to 100 facts, only ones you explicitly asked it to
remember. It never stores passwords, PINs or card numbers. A fact such as "call me Krishna" sets the
name it uses.

**Reminders** (`data/reminders.sqlite`). You can set them up to 30 days ahead, and they survive
restarts. One that falls due while JARVIS is closed is delivered at the next start, marked as late.

**Job search.** Ask "how's my job search?", "what's shortlisted?", "anything waiting for my
approval?", "what did I apply to today?", "any questions for me?" or "did my resume upload work?".
JARVIS asks JobPilot's local API (`job_search.api_url`, `http://127.0.0.1:8000/api`) and answers in a
few sentences: when the last search ran, new jobs, the shortlist and how many have tailored documents,
applications waiting in JobPilot's review queue or stopped at a security check, applications sent,
interviews and offers, and screening questions waiting for you. JobPilot's demo jobs are left out.
The LinkedIn resume upload status still comes from `job_search.sync_dir`. JobPilot must be running
(`E:\JobPilot\scripts\run_api.ps1`); if it isn't, JARVIS says so. It only reads: it never changes a
status, approves or applies.

**Morning briefing.** This is off by default; turn it on with `features.morning_briefing: true`. On
your first launch each day you get a spoken summary of under 45 seconds: the date, the weather (set
`weather.default_city`), three headlines and today's reminders.

## The wake word

The wake word runs offline, using openWakeWord's `hey_jarvis` model.

1. **Check it:** run `.\run.bat --mic-test` and say "hey jarvis" about five times at your normal
   volume. It shows your mic level and the wake score at several boost levels, and suggests a
   `wake_word.gain`. To test a different microphone, use `--mic "part of its name"`.
2. **If it misses you** (common with some accents), teach it your voice. This takes about 4 minutes:
   ```bash
   .\run.bat --train-wake
   ```
   Quit JARVIS first. Say "hey jarvis" 20 times (normally, softly, from further away and quickly),
   then talk normally for 40 seconds. Each run adds to your earlier recordings, and it reports how
   often it woke on recordings it didn't learn from. JARVIS trains a small personal
   *verifier* (`data/wake/`) that decides whether it was you saying it. It's asked every time, and
   needs to be a little surer when the stock model barely reacted (that happens with some accents).
   The recordings never leave your PC. To undo it, delete `data/wake/hey_jarvis_verifier.pkl` or
   set `wake_word.personal_verifier: false`.
3. While JARVIS is asleep, a line such as `(heard something like "hey jarvis" but not sure enough:
   stock model 0.30, your voice model 0.35; need 0.50)` means it heard you but wasn't sure; the two
   scores say which model hesitated. Every attempt, including quieter ones, is in `logs/app.log`
   as `wake word: woke …` or `wake word: no wake …`. Nothing at all means the mic isn't picking
   you up.

## Troubleshooting

**JARVIS doesn't hear me**
- Run `.\run.bat --mic-test`. If the level stays near 0:
  - In **Settings > Privacy & security > Microphone**, turn on *Microphone access* and *Let desktop
    apps access your microphone*.
  - Check the mic isn't muted.
  - Pick the right one with `audio.input_device`.
- If the level is fine but JARVIS doesn't wake, see [the wake word](#the-wake-word).

**It reacts to background noise**
- Noise suppression (`noise_suppression.enabled`) removes steady noise such as fans and hum.
  `--mic-test` shows the noise level before and after.
- If your voice sounds clipped, lower `noise_suppression.strength`, for example to 0.7.

**It interrupts itself or hears its own voice**
- On speakers, JARVIS ignores the mic while it talks; interrupt it with Ctrl+Alt+S. With headphones,
  just talk over it.
- `audio.echo_mode: auto` guesses which you're using from the output device's name. Set
  `headphones` or `speakers` if it guesses wrong.
- Bluetooth earbuds switch to call-quality sound while their mic is in use, so the laptop mic is
  often the better choice.

**"I've hit my usage limit"**
- The free Flash quota is small, and research and plans use it the most. JARVIS falls back through
  `models.flash_fallbacks`, then OpenRouter if configured.

**The connection keeps dropping**
- JARVIS reconnects by itself, resuming the conversation where it can. After 3 failures in 5
  minutes it tells you and waits a minute. Check your internet connection and `logs/app.log`.

**"The JARVIS browser profile is open in another Chrome window"**
- Close the `--browser-login` window, or any leftover JARVIS Chrome, and try again. If you can't see
  one, end the leftover `chrome.exe` processes in Task Manager.

**A song doesn't play in the YouTube Music app**
- JARVIS finds the app from its Start-menu shortcut. If you reinstalled the app, restart JARVIS.

**JARVIS can't find a button, or an app feels slow**
- Run `.\run.bat --bench-apps` with the app open. It shows, for each open app, how many buttons and
  fields JARVIS can see and how long a read takes. It never clicks or types, and shows no app text.
- An app with very few controls doesn't support UI Automation well. Use "what's on my screen?" there.
- Say "what can I click here?" to hear the names JARVIS sees, then use one of those names or numbers.
- A minimised app has nothing on screen: say "restore Notepad" first.

**Nothing appears after clicking the icon**
- Wait about 10 seconds, then check `logs/console.txt` and `logs/app.log`. For a live view, start
  JARVIS with `.\run.bat` instead.

**The taskbar shows two JARVIS buttons**
- Unpin JARVIS, then pin it again from the **desktop** icon. The desktop icon carries the taskbar
  ID; `.\run.bat --install-shortcut` recreates it if needed.

**Logs**
- `logs/app.log`: everything, including the conversation, errors, button clicks and shutdown timing.
- `logs/console.txt`: what the terminal would show, when JARVIS runs from an icon.
- `logs/actions.jsonl`: the audit log of every tool call.
