# Suite: web smoke

## Case: home page greets the visitor
The landing page must welcome a new visitor.
Goto: /index.html
Expect-text: Welcome
Expect-no: 404

## Case: about link reaches the about page
Goto: /index.html
Click: text=About
Expect-url: about.html
Expect-text: About this app
