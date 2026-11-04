"use client";
import { ActivityFeed } from "@/components/ActivityFeed";
import { useShell } from "@/components/Shell";
import { PageTitle, Panel } from "@/components/ui";

export default function Activity() {
  const { live } = useShell();
  return (
    <>
      <PageTitle title="Activity Logs" sub={live ? "Streaming live from the agents. New events appear at the top." : "Reconnecting to the live stream…"} />
      <Panel><ActivityFeed limit={200} /></Panel>
    </>
  );
}
