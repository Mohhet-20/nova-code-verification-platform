import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";
import { setStoredToken } from "../services/api";

// Role queries over a full page are slow in jsdom; the 1 s default made findBy* calls flaky when the whole suite runs in parallel on a busy machine.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => {
  cleanup();
  setStoredToken("reset"); // a stored token also clears the module's "sign-in required" flag between tests
  setStoredToken("");
});
