import { useState } from "react";
import { Nav, type Page } from "./components/Nav";
import { Dashboard } from "./pages/Dashboard";
import { Chat } from "./pages/Chat";

export default function App() {
  const [page, setPage] = useState<Page>("dashboard");

  return (
    <div className="app">
      <Nav page={page} onNavigate={setPage} />
      {page === "dashboard" ? <Dashboard onNavigate={setPage} /> : <Chat />}
    </div>
  );
}
