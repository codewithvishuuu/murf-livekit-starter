import { AnalyticsDashboard } from '@/components/app/analytics/analytics-dashboard';
import { getCallAnalytics } from '@/lib/analytics';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

export default async function AnalyticsPage() {
  const initialData = await getCallAnalytics();

  return <AnalyticsDashboard initialData={initialData} />;
}
