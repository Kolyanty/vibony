import { useParams, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { SessionProvider } from '../state/SessionContext';
import { SessionView } from '../components/SessionView';

// Public, read-only replay accessed via a share token. Renders the same IDE
// layout as the live interviewer view (SessionView) in `shareToken` mode:
// config + events load through the share-token endpoints, no WS, no controls.
export function ReplayPage() {
  const { t } = useTranslation();
  const { code } = useParams<{ code: string }>();
  const [searchParams] = useSearchParams();
  const shareToken = searchParams.get('share_token');

  if (!code) {
    return (
      <main>
        <p>{t('replay.missingCode')}</p>
      </main>
    );
  }

  if (!shareToken) {
    return (
      <main>
        <p>{t('replay.missingToken')}</p>
      </main>
    );
  }

  return (
    <SessionProvider>
      <SessionView code={code} shareToken={shareToken} />
    </SessionProvider>
  );
}
