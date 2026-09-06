import { Tabs, TabsList, TabsTrigger } from "@/components/motion/tabs";

export type Page = "dashboard" | "chat";

export function Nav({ page, onNavigate }: { page: Page; onNavigate: (p: Page) => void }) {
  return (
    <nav className="nav">
      <span className="nav-brand">comp-use</span>
      {/* Controlled by the page the app is actually on, so the indicator can never
          disagree with what's rendered below it. TabsContent is deliberately
          unused - the panels are whole routed pages, not tab bodies. */}
      <Tabs value={page} onValueChange={(v) => onNavigate(v as Page)} variant="pill">
        <TabsList>
          <TabsTrigger value="dashboard">Dashboard</TabsTrigger>
          <TabsTrigger value="chat">Chat</TabsTrigger>
        </TabsList>
      </Tabs>
    </nav>
  );
}
