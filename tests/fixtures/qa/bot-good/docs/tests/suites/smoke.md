# Suite: bot smoke

## Case: /start greets the user
The bot must welcome a new user when they send /start.
Send: /start
Expect: welcome
Expect-no: error

## Case: /help lists the commands
Send: /help
Expect: commands

## Case: /echo echoes the argument back
Send: /echo hello there
Expect: hello there

## Case: unknown chatter is ignored
A plain message that is not a command must NOT get a reply.
Send: just some chatter
Expect-silent
