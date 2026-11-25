/**
 * Screenshots on the "How it works" page. Both are this UI rendering the e2e
 * fixtures, which the backend wrote by replaying two recorded CI runs through
 * its real pipeline (e2e/fixtures/README.md). Re-capture them when the UI
 * changes: 1280x800 viewport, device scale 2.
 */
import failedBuild from "./screens/failed-build.png";
import passedArtifacts from "./screens/passed-artifacts.png";

export interface Screen {
  src: string;
  alt: string;
  title: string;
  caption: string;
  width: number;
  height: number;
}

export const SCREENS: Screen[] = [
  {
    src: failedBuild,
    alt: "Build #2 failed in the test stage. The failure card shows the class test_failure, the first error at firmware/tests/test_threshold.c line 75, and the log with the failing lines highlighted.",
    title: "A failed build, explained.",
    caption:
      "The test stage failed. SiliconFlow classified it as test_failure, linked the first error to its file and line, and highlighted it in the log.",
    width: 2560,
    height: 1600,
  },
  {
    src: passedArtifacts,
    alt: "Build #1's artifacts tab in the dark theme: firmware.bin and the ELF with SHA-256 hashes computed by the server, both release-eligible, and the release gates table with all 11 checks passed.",
    title: "Verified files and release gates.",
    caption:
      "Every file's SHA-256 is computed by the server, and every gate is listed with what was measured, the limit and the result.",
    width: 2560,
    height: 1600,
  },
];
