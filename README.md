# clauthing
A soft-warm wrapper around claude code, using kitty and tmux.  Keep all you claude codes in one place.

![clauthing](picture.png)

THIS IS BUGGY ALPHA SOFTWARE; IT IS AI-GENERATED AND NOT WELL REVIEWED. It does however contain some magic and I wanted to throw it onto the internet so othes could use it.

Only tested on linux.

## Features

* :command to run *DETERMINISTIC actions written in code*
* :cd command to change directory
* :fork command to clone a conversation
* :permissions to view permissions
* :rule to modularly define context which is always laded.
* :skill SKILL to creeate a new school
* :reload to restart claude at the same point with new features
* ::skill new to define a template to be *deterministally added to the context at this point* using ::new

Other stuff.

Planned features. Use arbitrary terminals rather than kitty.

## Changelog

1.0.0 - Basic packaging up of one I am using. I was mostly using --one-tab
1.1.0 - Fixed a bunch of bugs in multi-window setting since I am using it. `:resume`, `:cd`, got multi logging working. End-to-end tests

## Motivation
claude code has a number of features that that I want which are absent from the claude desktop app. Mostly things related to programmatic access to messages, hooks surrounding messages being sent, and the ability to run deterministic commands.

However, I will likely still want to use a terminal even with claude code and I don't want claude code using up all my terminals. So I want to wrap up claude code in a kitty window running tmux away from terminals. 

As a side effect, wrapping claude with `tmux` lets me do lots of weird magic to add features.

## Alternatives and prior work
This is terrible. It is a wrapper around claude code and mostly implemented using tmux and kitty. However, it is usable and I use it and it gives you access to the claude code subscription while still using claude as as the underlying harness. 

This means you get access to claude's cheaper subscriptin models, with additional features, without violating their tems and conditions.

If you are willing to spend more money you can use API based usage rather than sunscription based usage and use something like opencode or PI. I have not used these myself as I want to use the cheaper model. Chatgpt is compatible with open code.

## Installation
`pipx install clauthing`

`Install kitty and tmux`

## Usage
`clauthing`

## Single window
I have a mode of development where I cycle through a lot of separate x11 windows. I use `--one-tab` for this.

## Shorcuts

  Tab Switching

  - Alt+h - Switch to previous window
  - Alt+\ - Switch to next window
  - Alt+o - Toggle to last window (switch between current and previous)

  Other Useful Keybindings

  - Ctrl+n - Open new window
  - Ctrl+w - Close current window (won't close if it's the last one)
  - Alt+r - reload the curent window

## Colon commands
There are some commands implemented with hooks. Type `:help` to see the commands.

## Doublecolon skills
Claude has skills but they are stuck in the mindset of "allow claude to do the orchestration". Colon skills are a clauthing specific feature. If you type `::blah` the blah skill is immediately sent to claude.

To create the skill `blah` you can use `::skill blah`.

## Session Storage
Session metadata is stored in `~/.local/state/clauthing/sessions/` and open sessions are tracked in `~/.config/clauthing/open-sessions.json` (for debugging purposes only - liable to change).

## Testing

Fast tests (no credentials needed):

```bash
./run-tests
```

Live tests run a real `claude` session and require a credential snapshot:

```bash
# One-time: open a browser OAuth flow and capture the resulting credentials
creds-for-claude get > /tmp/creds.json

# Verify the credentials work
creds-for-claude check < /tmp/creds.json

# Run the live :cd test
python live_tests/test_cd_live.py
```

`creds-for-claude` is a separate tool (`pipx install creds-for-claude`) that drives a
fresh `claude` through OAuth login in an isolated temp directory and captures the
resulting tokens — so live tests never touch your main claude config.

## Contributing
I'm vibe coding. You're vibe coding. I suggest you create a fork named clauthing-whatever and try to get people to use it. Send me your fork in a PR, describing what it does in as much detail as you can muster and I will have an LLM tell me what is going on and reimplement your idea.

# About
I am @readwith. As part of my work i make a stream of tools related to AI-coding and useing a computer in general. If this is interesting I suggest following me on github. If I am interesting, follow me on X.

---

Image from Lewis Carroll.
