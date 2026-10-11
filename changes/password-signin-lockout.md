### Fixed

- **A stranger's wrong passwords no longer lock out a browser that has signed in before or pause every password sign-in.** Browsers that have signed in before have their own guessing limits and their own password worker, a flood of strangers' tries is turned away before it costs a password check, and an existing session can confirm its password even while strangers guess its account.
