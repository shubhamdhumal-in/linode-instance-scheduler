import type { ReactElement } from 'react'
import { useEffect } from 'react'
import { Navigate, Route, HashRouter as Router, Routes } from 'react-router-dom'
import { cancelBackgroundPolling, resetBackgroundPollingCancellation } from './api/client'
import { AuthProvider, useAuth } from './auth/AuthContext'
import { ActivityPage } from './pages/ActivityPage'
import { PendingDeletionsPage } from './pages/PendingDeletionsPage'
import { BackupsPage } from './pages/BackupsPage'
import { ConsolePage } from './pages/ConsolePage'
import { DashboardLayout } from './pages/DashboardLayout'
import { GroupDetailPage } from './pages/GroupDetailPage'
import { GroupsPage } from './pages/GroupsPage'
import { HolidaysPage } from './pages/HolidaysPage'
import { InstanceDetailPage } from './pages/InstanceDetailPage'
import { InstancesPage } from './pages/InstancesPage'
import { LoginPage } from './pages/LoginPage'
import { LogsPage } from './pages/LogsPage'
import { MigratePage } from './pages/MigratePage'
import { OnboardPage } from './pages/OnboardPage'
import { SystemBackupPage } from './pages/SystemBackupPage'
import { TokensPage } from './pages/TokensPage'
import { StatusBarProvider } from './status/StatusBarContext'
function RequireAuth({ children }: { children: ReactElement }) {
  const { isAuthenticated } = useAuth()
  return isAuthenticated ? children : <Navigate to="/login" replace />
}
function AppRoutes() {
  const { isAuthenticated } = useAuth()
  useEffect(() => {
    if (isAuthenticated) resetBackgroundPollingCancellation()
    else cancelBackgroundPolling()
  }, [isAuthenticated])
  return (
    <Routes>
      <Route path="/login" element={isAuthenticated ? <Navigate to="/instances" replace /> : <LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <DashboardLayout />
          </RequireAuth>
        }
      >
        <Route path="/instances" element={<InstancesPage />} />
        <Route path="/onboard" element={<OnboardPage />} />
        <Route path="/migrate" element={<MigratePage />} />
        <Route path="/instances/:name" element={<InstanceDetailPage />} />
        <Route path="/groups" element={<GroupsPage />} />
        <Route path="/groups/:name" element={<GroupDetailPage />} />
        <Route path="/tokens" element={<TokensPage />} />
        <Route path="/holidays" element={<HolidaysPage />} />
        <Route path="/backups" element={<BackupsPage />} />
        <Route path="/pending-deletions" element={<PendingDeletionsPage />} />
        <Route path="/system-backup" element={<SystemBackupPage />} />
        <Route path="/activity" element={<ActivityPage />} />
        <Route path="/logs" element={<LogsPage />} />
        <Route path="/console" element={<ConsolePage />} />
      </Route>
      <Route path="*" element={<Navigate to="/instances" replace />} />
    </Routes>
  )
}
export default function App() {
  return (
    <Router>
      <AuthProvider>
        <StatusBarProvider>
          <AppRoutes />
        </StatusBarProvider>
      </AuthProvider>
    </Router>
  )
}
