import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaGamepad } from "react-icons/fa";

interface Composite {
  path: string;
  name: string | null;
  profile: string | null;
  sources: string[] | null;
  targets: string[] | null;
}

interface Status {
  available: boolean;
  error: string | null;
  version: string | null;
  composites: Composite[];
  selected: string | null;
  disabled: boolean;
  duplicates: string[];
  gamepads: { node: string; name: string }[];
  pad_nodes: string[];
  raw_holders: { pid: number; comm: string; node: string }[];
  rule_installed: boolean;
  failsafe: boolean;
  last_error: string | null;
}

interface Result {
  ok: boolean;
  error?: string;
}

const getStatus = callable<[], Status>("get_status");
const setFailsafe = callable<[enabled: boolean], boolean>("set_failsafe");
const disableController = callable<[], Result>("disable_controller");
const enableController = callable<[], Result>("enable_controller");
const restartInputPlumber = callable<[], Result>("restart_inputplumber");
const resetPad = callable<[], Result>("reset_pad");

// Field descriptions are HTML, so "\n" would collapse into a space.
const Lines = ({ lines }: { lines: string[] }) => (
  <>
    {lines.map((line, i) => (
      <div key={i}>{line}</div>
    ))}
  </>
);

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [showDiag, setShowDiag] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      // A crashed backend never answers, so don't wait forever.
      const timeout = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("Backend did not respond (see plugin logs)")), 20000)
      );
      setStatus(await Promise.race([getStatus(), timeout]));
      setLoadError(null);
    } catch (e) {
      console.error("A1X Controller: status failed", e);
      setLoadError(String(e));
    }
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, []);

  const run = async (fn: () => Promise<Result>, okMsg: string) => {
    setBusy(true);
    try {
      const r = await fn();
      toaster.toast({ title: "A1X Controller", body: r.ok ? okMsg : `Failed: ${r.error}` });
    } finally {
      setBusy(false);
      refresh();
    }
  };

  if (!status) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label={loadError ? "Backend error" : "Loading…"} description={loadError ?? undefined} />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (!status.available) {
    return (
      <PanelSection title="InputPlumber unavailable">
        <PanelSectionRow>
          <Field label="Error" description={status.error ?? "unknown"} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => run(restartInputPlumber, "InputPlumber restarted")}>
            Restart InputPlumber
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const otherPads = status.gamepads.length;

  return (
    <>
      <PanelSection title="Integrated Controller">
        <PanelSectionRow>
          <ToggleField
            label="Enabled"
            description={
              status.disabled
                ? "Stopped. Re-enabling restarts InputPlumber."
                : otherPads === 0
                ? "No other gamepad detected"
                : undefined
            }
            checked={!status.disabled}
            disabled={busy || !status.selected}
            onChange={(on) =>
              run(on ? enableController : disableController, on ? "Controller enabled" : "Controller disabled")
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Failsafe"
            description={`Re-enable automatically if no other gamepad is connected for 10s`}
            checked={status.failsafe}
            onChange={async (v) => {
              await setFailsafe(v);
              refresh();
            }}
          />
        </PanelSectionRow>
        {status.raw_holders.length > 0 && (
          <PanelSectionRow>
            <Field
              label="⚠ Raw pad open by another app"
              description={`${Array.from(new Set(status.raw_holders.map((h) => h.comm))).join(", ")} is reading the pad directly, so it shows up twice. Use Reset Pad.`}
            />
          </PanelSectionRow>
        )}
        {status.raw_holders.length > 0 && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => run(resetPad, "Pad reset")}>
              Reset Pad
            </ButtonItem>
          </PanelSectionRow>
        )}
        {!status.rule_installed && (
          <PanelSectionRow>
            <Field label="⚠ Hide rule not installed" description={status.last_error ?? "See plugin logs"} />
          </PanelSectionRow>
        )}
        {status.duplicates.length > 0 && (
          <PanelSectionRow>
            <Field
              label="⚠ Duplicate device detected"
              description={`${status.duplicates.join(", ")} appears more than once. Try Restart Controller.`}
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => run(restartInputPlumber, "Controller restarted")}
          >
            {busy ? "Working…" : "Restart Controller"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <ToggleField label="Show diagnostics" checked={showDiag} onChange={setShowDiag} />
        </PanelSectionRow>
      </PanelSection>

      {showDiag && (
        <PanelSection title="Diagnostics">
          <PanelSectionRow>
            <Field
              focusable
              label="Target"
              description={`${status.selected ?? "none found"}${status.disabled ? " (stopped)" : ""}`}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field focusable label="InputPlumber" description={`v${status.version}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field
              focusable
              label="Raw pad"
              description={
                <Lines
                  lines={[
                    `nodes: ${status.pad_nodes.join(", ") || "-"}`,
                    `hide rule: ${status.rule_installed ? "installed" : "missing"}`,
                    `open by: ${status.raw_holders.map((h) => `${h.comm}[${h.pid}] ${h.node}`).join(", ") || "nobody"}`,
                  ]}
                />
              }
            />
          </PanelSectionRow>
          {status.composites.map((c) => (
            <PanelSectionRow key={c.path}>
              <Field
                focusable
                label={`${c.path.split("/").pop()}: ${c.name}`}
                description={
                  <Lines
                    lines={[
                      `profile: ${c.profile ?? "-"}`,
                      `sources: ${(c.sources ?? []).join(", ") || "-"}`,
                      `targets: ${(c.targets ?? []).map((t) => t.split("/").pop()).join(", ") || "-"}`,
                    ]}
                  />
                }
              />
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <Field
              focusable
              label={`Gamepads visible to games (${status.gamepads.length})`}
              description={<Lines lines={status.gamepads.length ? status.gamepads.map((g) => `${g.node}: ${g.name}`) : ["none"]} />}
            />
          </PanelSectionRow>
          {status.last_error && (
            <PanelSectionRow>
              <Field focusable label="Last error" description={status.last_error} />
            </PanelSectionRow>
          )}
        </PanelSection>
      )}
    </>
  );
}

export default definePlugin(() => {
  const onFailsafe = () =>
    toaster.toast({ title: "A1X Controller", body: "No other gamepad found — integrated controller re-enabled" });
  addEventListener("failsafe_triggered", onFailsafe);

  return {
    name: "A1X Controller",
    titleView: <div className={staticClasses.Title}>A1X Controller</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {
      removeEventListener("failsafe_triggered", onFailsafe);
    },
  };
});
