import js from '@eslint/js';
import globals from 'globals';

export default [
  { ignores: ['src/switchboard/web/static/vendor/**'] },
  {
    files: ['src/switchboard/web/static/*.js'],
    languageOptions: { ecmaVersion: 'latest', sourceType: 'script', globals: globals.browser },
    rules: {
      ...js.configs.recommended.rules,
      eqeqeq: 'error',
      // Discarding a caught browser error is intentional on fallback paths; regular unused
      // bindings still fail. In particular, passkey errors are rewritten to safe text.
      'no-unused-vars': ['error', { caughtErrors: 'none' }],
    },
  },
];
