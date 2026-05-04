"use client";

import { Check, X } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";

export function ApprovalActions() {
  const [status, setStatus] = useState<"pending" | "approved" | "rejected">("pending");

  if (status !== "pending") {
    return <span className="text-sm font-black capitalize text-muted">{status}</span>;
  }

  return (
    <div className="flex gap-2">
      <Button type="button" size="sm" variant="outline" onClick={() => setStatus("approved")}>
        <Check className="h-4 w-4" />
        Approve
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={() => setStatus("rejected")}>
        <X className="h-4 w-4" />
        Reject
      </Button>
    </div>
  );
}

