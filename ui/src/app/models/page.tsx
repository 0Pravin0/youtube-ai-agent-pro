"use client";
import { ComputerCheck, EngineControls, ToolsList } from "@/components/FreeTools";
import { useShell } from "@/components/Shell";
import { PageTitle, Panel } from "@/components/ui";

export default function Models() {
  const { toast } = useShell();
  return (
    <div className="space-y-6">
      <PageTitle title="Model Manager" sub="Local AI models for writing and pictures. Install the recommended ones for this Mac, or switch between installed models." />
      <Panel title="This Mac"><ComputerCheck compact /></Panel>
      <Panel title="Running engines"><EngineControls toast={toast} /></Panel>
      <Panel title="Models"><ToolsList toast={toast} groups={["writer_model", "image_model"]} /></Panel>
    </div>
  );
}
