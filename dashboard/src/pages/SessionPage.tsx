import { useLocation, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { SessionProvider } from '../state/SessionContext';
import { SessionView } from '../components/SessionView';

export function SessionPage() {
  const { t } = useTranslation();
  const { code } = useParams<{ code: string }>();
  const location = useLocation();
  const candidateToken = (location.state as { candidateToken?: string } | null)?.candidateToken;

  if (!code) {
    return (
      <main>
        <p>{t('session.missingCode')}</p>
      </main>
    );
  }

  return (
    <SessionProvider>
      <SessionView code={code} candidateToken={candidateToken} />
    </SessionProvider>
  );
}
