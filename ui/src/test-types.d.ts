// Loads the jest-dom matcher types into the `tsc` program.  vitest.setup.ts
// lives outside src/ (and out of tsconfig's include), so without this file the
// test files' `expect(...).toBeInTheDocument()` would fail typecheck despite
// the runtime augmentation working under vitest.
import '@testing-library/jest-dom/vitest';
