import { useCallback, useEffect, useRef, useState } from "react";

const MIN_WIDTH = 220;
const DEFAULT_WIDTH = 280;
const STORAGE_KEY = "comp-use:sidebar-width";

/** A third of the viewport, the cap the sidebar may be dragged to. Recomputed on
 *  resize so the cap follows the window rather than whatever it was on load. */
function maxWidth(): number {
  return Math.round(window.innerWidth / 3);
}

/** Sidebar with a drag handle on its trailing edge.
 *
 *  Width is clamped between a readable minimum and one third of the viewport, and
 *  remembered across reloads - a capability list is something people widen once
 *  to read long names and then expect to stay that way. The clamp is re-applied
 *  on window resize, so a width chosen on a wide monitor cannot swallow a
 *  narrower one.
 */
export function ResizableSidebar({ children }: { children: React.ReactNode }) {
  const [width, setWidth] = useState(() => {
    const stored = Number(localStorage.getItem(STORAGE_KEY));
    return Number.isFinite(stored) && stored >= MIN_WIDTH ? stored : DEFAULT_WIDTH;
  });
  const [dragging, setDragging] = useState(false);
  const asideRef = useRef<HTMLElement>(null);

  const clamp = useCallback((next: number) => Math.max(MIN_WIDTH, Math.min(next, maxWidth())), []);

  // Persist only settled widths, not every pixel of a drag.
  useEffect(() => {
    if (dragging) return;
    localStorage.setItem(STORAGE_KEY, String(width));
  }, [dragging, width]);

  useEffect(() => {
    const onResize = () => setWidth((w) => clamp(w));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [clamp]);

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: PointerEvent | MouseEvent) => {
      const left = asideRef.current?.getBoundingClientRect().left ?? 0;
      setWidth(clamp(e.clientX - left));
    };
    const stop = () => setDragging(false);
    // Both families: pointer events are the modern path (and cover touch/pen),
    // but plain mouse events still reach us from automation and from anything
    // that doesn't synthesise pointer events - without them the handle silently
    // does nothing.
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stop);
    // While dragging, suppress text selection and keep the resize cursor even as
    // the pointer outruns the 4px handle.
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", stop);
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
    };
  }, [clamp, dragging]);

  return (
    <aside ref={asideRef} className="sidebar" style={{ width }}>
      <div className="sidebar-scroll">{children}</div>
      <div
        className={`sidebar-resizer${dragging ? " is-dragging" : ""}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize sidebar"
        aria-valuenow={width}
        aria-valuemin={MIN_WIDTH}
        aria-valuemax={maxWidth()}
        tabIndex={0}
        onPointerDown={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onMouseDown={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDoubleClick={() => setWidth(DEFAULT_WIDTH)}
        // Keyboard resizing, so the panel isn't pointer-only.
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") setWidth((w) => clamp(w - 16));
          if (e.key === "ArrowRight") setWidth((w) => clamp(w + 16));
        }}
      />
    </aside>
  );
}
