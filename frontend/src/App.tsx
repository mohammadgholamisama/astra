import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./store/auth";
import Layout from "./components/Layout";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";
import PanelsPage from "./pages/PanelsPage";
import GroupsPage from "./pages/GroupsPage";
import UsersPage from "./pages/UsersPage";
import BackupPage from "./pages/BackupPage";
import SettingsPage from "./pages/SettingsPage";
import ConfigsPage from "./pages/ConfigsPage";
import ConfigOrderPage from "./pages/ConfigOrderPage";

function Private({ children }: { children: React.ReactNode }) {
  const token = useAuth((s) => s.token);
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <Private>
            <Layout />
          </Private>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="panels" element={<PanelsPage />} />
        <Route path="groups" element={<GroupsPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="configs" element={<ConfigsPage />} />
        <Route path="config-order" element={<ConfigOrderPage />} />
        <Route path="backup" element={<BackupPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
