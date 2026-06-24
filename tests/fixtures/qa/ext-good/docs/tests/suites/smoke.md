# Suite: ext smoke

## Case: hello command greets the user
The myext.hello command must show a 'Hello' notification.
Command: myext.hello
Expect-notification: Hello
Expect-no: error

## Case: hello notification names the extension
Command: myext.hello
Expect-notification: from myext
