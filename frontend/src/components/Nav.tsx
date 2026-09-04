export type Page = "dashboard" | "chat";

export function Nav({ page, onNavigate }: { page: Page; onNavigate: (p: Page) => void }) {
  return (
    <nav className="nav">
      <span className="nav-brand">comp-use</span>
      <button className={page === "dashboard" ? "nav-link active" : "nav-link"} onClick={() => onNavigate("dashboard")}>
        Dashboard
      </button>
      <button className={page === "chat" ? "nav-link active" : "nav-link"} onClick={() => onNavigate("chat")}>
        Chat
      </button>
    </nav>
  );
}
