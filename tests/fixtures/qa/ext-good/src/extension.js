// A tiny VS Code extension fixture (GOOD): the myext.hello command shows a "Hello from myext"
// information message — the notification the smoke suite asserts on. The behavior.json next to
// this file mirrors the same outcome for the DETERMINISTIC CI fake (which never launches VS
// Code); the two are kept in sync so the live leg and the deterministic leg verdict the same
// thing.
const vscode = require('vscode');

function activate(context) {
  const disposable = vscode.commands.registerCommand('myext.hello', function () {
    vscode.window.showInformationMessage('Hello from myext');
  });
  context.subscriptions.push(disposable);
}

function deactivate() {}

module.exports = { activate, deactivate };
