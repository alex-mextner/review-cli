// The BUGGY VS Code extension fixture: the myext.hello command shows the WRONG notification
// ("Goodbye, cruel world") instead of the expected "Hello from myext", so the smoke suite's
// Expect-notification: Hello / from myext both miss and the harness verdicts FAIL with a finding.
// behavior.json mirrors the same wrong outcome for the DETERMINISTIC CI fake.
const vscode = require('vscode');

function activate(context) {
  const disposable = vscode.commands.registerCommand('myext.hello', function () {
    vscode.window.showInformationMessage('Goodbye, cruel world');
  });
  context.subscriptions.push(disposable);
}

function deactivate() {}

module.exports = { activate, deactivate };
