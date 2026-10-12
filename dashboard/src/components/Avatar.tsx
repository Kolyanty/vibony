import { Bot, UserRound, Wrench } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import styles from './Avatar.module.css';

export type AvatarRole = 'candidate' | 'ai' | 'tool';

interface AvatarProps {
  role: AvatarRole;
  size?: 'md' | 'sm';
}

const ICON_BY_ROLE = {
  candidate: UserRound,
  ai: Bot,
  tool: Wrench,
} as const;

const LABEL_KEY_BY_ROLE: Record<AvatarRole, string> = {
  candidate: 'role.aria.candidate',
  ai: 'role.aria.ai',
  tool: 'role.aria.tool',
};

export function Avatar({ role, size = 'md' }: AvatarProps) {
  const { t } = useTranslation();
  const Icon = ICON_BY_ROLE[role];
  const iconPx = size === 'sm' ? 14 : 18;
  return (
    <div
      className={styles.avatar}
      data-role={role}
      data-size={size}
      aria-label={t(LABEL_KEY_BY_ROLE[role])}
      role="img"
    >
      <Icon size={iconPx} strokeWidth={2} aria-hidden="true" />
    </div>
  );
}
