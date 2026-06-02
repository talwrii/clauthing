# Claudes

clauthing works by wrapping a number of claude instances. Each claude instance
has a session id, which refers to some metadata about the session.

At the same time we have windows, each of which runs a claude instance. However,
because of how claude works, we may need to change the session-id used in a
particular window. session-ids cannot change their directory, while a window can
change directory with the `:cd` command.

We use a window-id to refer to a window open in clauthing. This will have a
particular session-id associated with it, but this can change.
