import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { extractErrorMessage } from "@/api/client";
import { analyticsApi } from "@/api/endpoints";
import { PageHeader } from "@/components/AppShell";

export function AnalyticsPage() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["analytics"],
    queryFn: () => analyticsApi.dashboard(),
    staleTime: 60_000,
  });

  return (
    <div>
      <PageHeader
        title="Analytics"
        subtitle="Snapshot of the FastRecce lead funnel"
      />

      {isLoading ? (
        <div className="grid animate-pulse grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-24 rounded-md bg-muted" />
          ))}
        </div>
      ) : isError || !data ? (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 p-4 text-sm">
          Failed to load analytics: {extractErrorMessage(error)}
        </div>
      ) : (
        <div className="space-y-6">
          <section className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <StatCard label="Total properties" value={data.properties.total} />
            <StatCard
              label="New"
              value={data.properties.by_status.new ?? 0}
            />
            <StatCard
              label="Approved"
              value={data.properties.by_status.approved ?? 0}
            />
            <StatCard
              label="LLM briefs"
              value={data.llm.briefed}
            />
          </section>

          <section className="grid gap-6 md:grid-cols-2">
            <Card title="Properties by city">
              <ChartBars data={data.properties.by_city} />
            </Card>
            <Card title="Properties by type">
              <ChartBars data={data.properties.by_type} />
            </Card>
          </section>

          <section className="grid gap-6 md:grid-cols-2">
            <Card title="LLM coverage">
              <StatRow label="Properties scored" value={data.llm.scored} />
              <StatRow label="Properties briefed" value={data.llm.briefed} />
              <StatRow
                label="Coverage"
                value={`${Math.round(
                  ((data.llm.briefed || 0) / Math.max(1, data.properties.total)) * 100
                )}%`}
              />
            </Card>
          </section>
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md border border-border bg-background p-5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-3xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function Card({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-md border border-border bg-background p-5">
      <h3 className="mb-3 text-sm font-medium text-muted-foreground">{title}</h3>
      {children}
    </div>
  );
}

function StatRow({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex items-center justify-between border-b border-border py-2 last:border-b-0">
      <span className="text-sm">{label}</span>
      <span className="text-sm font-semibold tabular-nums">{value}</span>
    </div>
  );
}

function ChartBars({ data }: { data: Record<string, number> }) {
  const rows = Object.entries(data).map(([name, value]) => ({
    name: name.replace(/_/g, " "),
    value,
  }));
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No data yet.</p>;
  }
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} layout="vertical" margin={{ left: 20, right: 10 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis type="number" fontSize={11} />
          <YAxis
            type="category"
            dataKey="name"
            fontSize={11}
            width={100}
            interval={0}
          />
          <Tooltip cursor={{ fill: "rgba(0,0,0,0.04)" }} />
          <Bar dataKey="value" fill="currentColor" className="text-primary" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

