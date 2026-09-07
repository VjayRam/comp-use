import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface SelectOption {
  value: string;
  label: string;
  /** Rendered apart from the main list, under a divider (e.g. "+ Add new site…"). */
  footer?: boolean;
}

/**
 * A listbox that replaces a native <select>.
 *
 * The only reason this exists is the options panel: a native select's popup is
 * drawn by the operating system, not the page, so no amount of CSS on `select`
 * or `option` gives it this app's panel background, rounded corners, or hover
 * state - Chrome on Windows renders a flat white system menu regardless. The
 * trigger below is styled the same as the old select was; everything new is the
 * panel.
 *
 * Keyboard behaviour follows the ARIA listbox pattern, since replacing a native
 * control means re-implementing what it gave for free: Enter/Space/Arrow opens,
 * arrows and Home/End move the active option, Enter/Space commits it, Escape
 * closes without committing, and Tab or an outside click closes as well.
 */
export function SelectMenu({
  id,
  value,
  options,
  placeholder,
  onChange,
}: {
  id: string;
  value: string;
  options: SelectOption[];
  placeholder: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Which option the keyboard is on. Distinct from `value`: moving through the
  // list must not commit anything until Enter, or arrowing past a destructive
  // entry would fire it in passing.
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const selected = options.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  // Scroll the active option into view, before paint so a held arrow key doesn't
  // visibly lag the highlight.
  useLayoutEffect(() => {
    if (!open || activeIndex < 0) return;
    const el = listRef.current?.children[activeIndex] as HTMLElement | undefined;
    el?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex]);

  function openWith(index: number) {
    setActiveIndex(index);
    setOpen(true);
  }

  function commit(index: number) {
    const option = options[index];
    if (!option) return;
    setOpen(false);
    // Re-selecting the current value is a no-op upstream, but selectApp() tears
    // down the session, so it must not be called for a no-op selection.
    if (option.value !== value) onChange(option.value);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const selectedIndex = options.findIndex((o) => o.value === value);
    if (!open) {
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        openWith(selectedIndex >= 0 ? selectedIndex : 0);
      }
      return;
    }
    if (e.key === "Escape" || e.key === "Tab") {
      setOpen(false);
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      commit(activeIndex);
      return;
    }
    const moves: Record<string, number> = { ArrowDown: 1, ArrowUp: -1 };
    if (e.key in moves) {
      e.preventDefault();
      const next = activeIndex + moves[e.key];
      // Clamped, not wrapping: wrapping from the last entry lands on the
      // placeholder-adjacent top of a list whose last entry is "+ Add new site…".
      setActiveIndex(Math.min(options.length - 1, Math.max(0, next)));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActiveIndex(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActiveIndex(options.length - 1);
    }
  }

  return (
    <div className="select-menu" ref={rootRef}>
      <button
        type="button"
        id={id}
        className="select-menu-trigger"
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={`${id}-listbox`}
        onClick={() => (open ? setOpen(false) : openWith(options.findIndex((o) => o.value === value)))}
        onKeyDown={onKeyDown}
      >
        <span className={selected ? undefined : "select-menu-placeholder"}>
          {selected?.label ?? placeholder}
        </span>
        <span className="select-menu-caret" aria-hidden="true" />
      </button>
      {open && (
        <ul
          id={`${id}-listbox`}
          className="select-menu-panel"
          role="listbox"
          ref={listRef}
          aria-activedescendant={activeIndex >= 0 ? `${id}-opt-${activeIndex}` : undefined}
        >
          {options.map((o, i) => (
            <li
              key={o.value}
              id={`${id}-opt-${i}`}
              role="option"
              aria-selected={o.value === value}
              className={[
                "select-menu-option",
                o.value === value ? "is-selected" : "",
                i === activeIndex ? "is-active" : "",
                o.footer ? "is-footer" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              // Pointer selection commits on mousedown's sibling event rather than
              // click so it beats the outside-click handler above to the punch.
              onMouseEnter={() => setActiveIndex(i)}
              onClick={() => commit(i)}
            >
              {o.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
