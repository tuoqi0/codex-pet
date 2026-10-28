# Codex Pet

A desktop companion with an original dragon character and an optional Q-style
white dragon. Both windows are independently draggable and share a conversation,
task and quota panel. The seated dragon follows the supplied reference, blinks,
greets, reacts happily, drinks tea and reads while working. Her wings and tail
sway through a mesh warp of the flat illustration;
they are not independently rigged. Patrol moves the figure as a whole.

## Install or upgrade

Build with `./build-deb.sh`. Install the resulting package with:

```sh
sudo apt install ./dist/codex-pet_2.5.0_all.deb
```

Quit an already-running older version through its right-click menu before
starting the new version. Open **Codex Pet** from the application menu, or run
`codex-pet` in a terminal. The package depends on PyQt5, Pillow, Requests,
PyJWT and cryptography; apt installs these dependencies.

The third-party Q character art is not redistributed in the package or Git.
To use the copy already supplied by the user, import it once:

```sh
codex-pet-import /home/tq/codex-pet.tar.xz
```

This imports only the character's WebP files, catalogue and provenance into
`${XDG_DATA_HOME:-$HOME/.local/share}/codex-pet/bailong`. It refuses to overwrite
an existing directory. Restart the pet after importing. If the art is absent or
unusable, the original dragon still runs, and the terminal shows a reason.

## Interaction

- Click either character for a greeting; double-click to open the floating
  controls with Codex quota, chat, plan, task and companion settings. Drag to
  move it. The Q character
  plays drag and landing sequences; its animations use the catalogue's timing
  and alignment. The player keeps one Q animation source and one displayed frame at a time; the WebP decoder may use additional working memory.
- Right-click either character to open the shared panel, view quota, change that
  character's size, hide or restore the other character, or quit. The final
  visible character cannot be hidden so the controls remain accessible.
- The dragon has an optional desktop patrol. Q
  animations for work, completion and quota are connected to panel events. When
  both are visible, nearby and idle on X11, they occasionally greet each other
  and can respond to a user greeting the other character.
  Automatic proximity interaction is disabled on Wayland, where absolute
  window positions are not reliable.

Companion settings offer optional rest, water, meal and greeting reminders,
quiet hours and a one-hour snooze. Reminders use the pet's runtime and the
local clock; they do not monitor activity or know whether someone is working,
drinking or eating. Weather is disabled until a city is explicitly searched
and selected in settings. Queries go to Open-Meteo and report model time and
source; the app does not use automatic location detection. The floating panel
offers manual weather refresh. Quiet hours suppress spoken prompts, not weather
data refreshes.

Transparent windows require a desktop compositor; transparent portions can
still receive clicks and a window manager can ignore always-on-top hints. On
native Wayland automatic patrol is disabled, and dragging uses the compositor's
system move; its availability and window positioning depend on the compositor.

## Chat, planning, tasks and usage

Open **對話與任務** or **查看額度** from either character's right-click menu.
The three modes explicitly use different routes:

- **普通對話** uses this app's Sign in with ChatGPT authorization and sends a
  streamed text request without local tools.
- **計劃** uses that authorization with Codex App Server in Plan mode and a
  read-only sandbox. Select a work directory first. The plan server uses a
  separate configuration directory, with external integrations disabled.
- **任務** uses the existing Codex ChatGPT login, asks for a work directory,
  and exposes supported approval requests. An API-key login is rejected.

Click **登入 ChatGPT** to authorize this application in a browser. Credentials
are stored in `~/.config/codex-pet/` with owner-only file permissions, separate
from Codex's own credentials. If available, email addresses are compared; a
mismatch is rejected. An absent email or unverified workspace is reported, not
treated as a verified match. The panel lists models returned for each route and
offers supported reasoning levels for Codex task models.

In task mode, **選擇 Codex 對話** lists local Codex sessions on request. Select a
session to read its messages, then explicitly choose the permissions for the
next turn before continuing. The app checks the returned sandbox and approval
policy; it does not infer the old session's permissions. Avoid running two
clients on the same session at once. ChatGPT authorization does not grant access
to ChatGPT web conversation history, so ordinary chat cannot synchronize that
history. Ordinary chat, plan and task retain separate contexts.

All three routes use ChatGPT plan access; none silently falls back to an API
key. Plan usage may share Codex or application limits, and purchased ChatGPT
credits can apply according to account settings. Codex quota is displayed only
when returned by App Server; the usage settings link opens ChatGPT's controls.
The authorized plan route is subject to OpenAI's eligibility and preview
limitations. Authorization does not grant access to ChatGPT conversation history.

## Development

Run `/usr/bin/python3 src/codex_pet.py`. Tests:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen /usr/bin/python3 src/codex_pet.py --quit-after-ms 4000
```

The generated seated-dragon PNG poses in `assets/` are included in the package.
User-imported Q art remains outside the package and its original permissions apply.
