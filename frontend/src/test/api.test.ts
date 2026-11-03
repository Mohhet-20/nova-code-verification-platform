import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, describeError, setStoredToken } from "../services/api";
import { formatBytes, splitLines } from "../services/format";
import { mockApi } from "./helpers";

afterEach(() => { vi.unstubAllGlobals(); setStoredToken(""); });

describe("api client", () => {
  it("parses the backend error envelope", async () => {
    mockApi({ "GET /repositories/x": { status: 404, body: { error: { code: "not_found", message: "Repository not found." } } } });
    const err = await api.getRepository("x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, code: "not_found", message: "Repository not found." });
  });

  it("includes field messages from validation errors", () => {
    const e = new ApiError(422, "validation_error", "Request validation failed.", [{ field: "title", message: "too short" }]);
    expect(describeError(e)).toBe("Request validation failed. title: too short");
  });

  it("reports network failures clearly", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("fail"); }));
    await expect(api.overview()).rejects.toMatchObject({ code: "network_error" });
  });

  it("sends the bearer token only when set", async () => {
    const { fn } = mockApi({ "GET /workspace": { body: {} } });
    await api.workspace();
    expect(new Headers(fn.mock.calls[0]?.[1]?.headers).has("Authorization")).toBe(false);
    setStoredToken("abc");
    await api.workspace();
    expect(new Headers(fn.mock.calls[1]?.[1]?.headers).get("Authorization")).toBe("Bearer abc");
  });

  it("omits empty query parameters", async () => {
    const { fn } = mockApi({ "GET /repositories/r1/files": { body: { items: [], total: 0, page: 1, page_size: 25, pages: 1 } } });
    await api.listFiles("r1", { page: 1, pageSize: 25, q: "", language: "Python" });
    expect(String(fn.mock.calls[0]?.[0])).toBe("/api/v1/repositories/r1/files?page=1&page_size=25&language=Python");
  });
});

describe("format helpers", () => {
  it("formats bytes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
  });
  it("splits and trims lines", () => {
    expect(splitLines(" a \n\n b\n")).toEqual(["a", "b"]);
  });
});
