import { Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { ScanProvider } from "./hooks/useScanContext";
import Sidebar from "./components/Sidebar";
import LoginPage from "./pages/auth/LoginPage";
import SignupPage from "./pages/auth/SignupPage";
import SelectOrgPage from "./pages/auth/SelectOrgPage";
import OverviewPage from "./pages/OverviewPage";
import FeaturesPage from "./pages/FeaturesPage";
import ImpactPage from "./pages/ImpactPage";
import IntegrationsPage from "./pages/IntegrationsPage";
import GitHubAppPage from "./pages/GitHubAppPage";
import ApiKeysPage from "./pages/ApiKeysPage";
import ProjectSettingsPage from "./pages/ProjectSettingsPage";
import TeamPage from "./pages/TeamPage";
import BillingPage from "./pages/BillingPage";

function ProtectedLayout() {
  const { isAuthenticated, activeOrg } = useAuth();

  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (!activeOrg) return <Navigate to="/select-org" replace />;

  return (
    <ScanProvider>
    <div className="app-layout">
      <Sidebar />
      <main className="app-main">
        <Routes>
          <Route path="/overview" element={<OverviewPage />} />
          <Route path="/features" element={<FeaturesPage />} />
          <Route path="/impact" element={<ImpactPage />} />
          <Route path="/integrations" element={<IntegrationsPage />} />
          <Route path="/github-app" element={<GitHubAppPage />} />
          <Route path="/settings" element={<ProjectSettingsPage />} />
          <Route path="/settings/api-keys" element={<ApiKeysPage />} />
          <Route path="/settings/team" element={<TeamPage />} />
          <Route path="/settings/billing" element={<BillingPage />} />
          <Route path="*" element={<Navigate to="/overview" replace />} />
        </Routes>
      </main>
    </div>
    </ScanProvider>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/select-org" element={<SelectOrgPage />} />
        <Route path="/*" element={<ProtectedLayout />} />
      </Routes>
    </AuthProvider>
  );
}
