import type { BuildDetail } from "../../api/client";
import { versionChips } from "../../format";

/** SDK and toolchain versions from the build's run report, SDKs first. */
export function VersionChips({ build }: { build: BuildDetail }) {
  const groups = [
    { kind: "SDK", chips: versionChips(build.sdk) },
    { kind: "toolchain", chips: versionChips(build.toolchain) },
  ];
  return (
    <>
      {groups.flatMap(({ kind, chips }) =>
        chips.map((c) => (
          <span key={`${kind}:${c.name}`} className={`chip-static mono${kind === "SDK" ? " chip-static--sdk" : ""}`} title={`${kind}: ${c.raw}`}>
            {c.name} {c.version}
          </span>
        )),
      )}
    </>
  );
}

export function hasVersions(build: BuildDetail): boolean {
  return versionChips(build.sdk).length > 0 || versionChips(build.toolchain).length > 0;
}
