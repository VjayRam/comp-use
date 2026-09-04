import { useEffect, useRef, useState } from "react";

// The sandbox's Xvfb display is a fixed size (see comp_use/sandbox.py's
// SandboxConfig.width/height); scale the noVNC iframe down to fit the panel width
// rather than showing it at native size or cropping it. Adapted from a pattern
// originally built for Project-Hawkeye's own live-run viewer - the scaling technique
// carried over, the rest (data fetching, layout, styling) is new here.
const BROWSER_W = 1366;
const BROWSER_H = 768;
const ASPECT = BROWSER_H / BROWSER_W;

export function BrowserFeedFrame({ url, interactive }: { url: string; interactive: boolean }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(0);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const obs = new ResizeObserver(([entry]) => {
      const w = entry.contentRect.width;
      const scale = w / BROWSER_W;
      setHeight(w * ASPECT);
      const iframe = iframeRef.current;
      if (!iframe) return;
      iframe.style.width = `${BROWSER_W}px`;
      iframe.style.height = `${BROWSER_H}px`;
      iframe.style.transform = `scale(${scale})`;
      iframe.style.transformOrigin = "top left";
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  return (
    <div ref={wrapRef} className="browser-feed" style={{ height }}>
      {/* url already redirects to vnc_lite.html?path=websockify&autoconnect=1&reconnect=1
          (see sandbox_image/novnc-index.html) - passed through as-is. */}
      <iframe ref={iframeRef} src={url} title="Live browser feed" />
      {/* The sandbox's x11vnc has no -viewonly flag (removed deliberately so a human
          CAN drive it during escalation/takeover) - without this overlay, every
          viewer's clicks/keystrokes would reach the live browser at all times,
          including mid-automation, and could silently derail a discovery/replay
          run in progress. Only lifted (pointer-events: none, clicks pass through
          to the iframe) while the run is actually escalated - agent-triggered or
          via the "Take control" button below, which is the only thing that can
          make `interactive` true. */}
      {!interactive && (
        <div className="browser-feed-block">
          <span>Agent is in control — use "Take control" to interact</span>
        </div>
      )}
    </div>
  );
}
