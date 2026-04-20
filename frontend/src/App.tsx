import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/AppShell";
import { AnalyticsPage } from "@/pages/Analytics";
import { LeadQueuePage } from "@/pages/LeadQueue";
import { PropertyDetailPage } from "@/pages/PropertyDetail";
import { PublicPropertyDetailPage } from "@/pages/PublicPropertyDetail";
import { SearchLandingPage } from "@/pages/SearchLanding";
import { SearchResultsPage } from "@/pages/SearchResults";

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/search" replace />} />
        <Route path="/search" element={<SearchLandingPage />} />
        <Route path="/search/results" element={<SearchResultsPage />} />
        <Route path="/search/property/:id" element={<PublicPropertyDetailPage />} />

        <Route path="/leads" element={<LeadQueuePage />} />
        <Route path="/property/:id" element={<PropertyDetailPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />

        {/* Legacy redirects */}
        <Route path="/admin" element={<Navigate to="/leads" replace />} />
        <Route path="/admin/leads" element={<Navigate to="/leads" replace />} />
        <Route path="/admin/analytics" element={<Navigate to="/analytics" replace />} />
        <Route path="/outreach" element={<Navigate to="/leads" replace />} />
        <Route path="/admin/outreach" element={<Navigate to="/leads" replace />} />
        <Route path="/admin/properties/:id" element={<LegacyAdminPropertyRedirect />} />
        <Route path="/properties/:id" element={<LegacyPropertyRedirect />} />
      </Route>

      <Route path="*" element={<Navigate to="/search" replace />} />
    </Routes>
  );
}

function LegacyPropertyRedirect() {
  const path = window.location.pathname.replace("/properties/", "/property/");
  return <Navigate to={path} replace />;
}

function LegacyAdminPropertyRedirect() {
  const path = window.location.pathname.replace("/admin/properties/", "/property/");
  return <Navigate to={path} replace />;
}
