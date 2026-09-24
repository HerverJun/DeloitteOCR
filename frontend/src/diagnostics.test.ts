import { describe, expect, it } from "vitest";
import { formatCapacity, gpuDescription, parseDoctorReport } from "./diagnostics";

describe("environment diagnostics presentation", () => {
  it("reads the doctor output without relying on property order", () => {
    expect(parseDoctorReport(JSON.stringify({
      gpu_exit: 0, missing: [], runtimes: { ppocr: true, glm: false },
      platform: "Windows", disk_free_bytes: 4 * 1024 ** 3,
    }))).toMatchObject({ gpu_exit: 0, runtimes: { ppocr: true, glm: false } });
  });

  it("does not present malformed or incomplete output as a verified check", () => {
    expect(parseDoctorReport("not json")).toBeNull();
    expect(parseDoctorReport("{}")).toBeNull();
    expect(parseDoctorReport(JSON.stringify({ gpu_exit: 0, missing: [], runtimes: { ppocr: "yes" } }))).toBeNull();
  });

  it("formats capacities and GPU information for people", () => {
    expect(formatCapacity(13.5 * 1024 ** 3)).toBe("13.5 GiB");
    expect(formatCapacity(undefined)).toBe("未获取");
    expect(gpuDescription("RTX 4070, 595.97, 16376 MiB, 13284 MiB"))
      .toBe("RTX 4070 · 驱动 595.97 · 显存空闲 13284 MiB / 16376 MiB");
  });
});
