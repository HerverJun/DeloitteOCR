import { Link } from 'react-router-dom'

export function ToolIdentity({ appId, to, label }: { appId: string; to: string; label: string }) {
  return <Link to={to} className="application-identity">
    <span className="application-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      {appId === 'data' ? <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 10h18M9 10v10M15 10v10" /></> :
        appId === 'face' ? <><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4" /><circle cx="12" cy="10" r="3" /><path d="M7 18c1-5 9-5 10 0" /></> :
        appId === 'audio' ? <><rect x="9" y="3" width="6" height="12" rx="3" /><path d="M6 10v2a6 6 0 0 0 12 0v-2M12 18v3M8 21h8" /></> :
        appId === 'ocr' ? <><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M8 8h8M8 12h8M8 16h5" /></> :
        appId === 'classification' ? <><rect x="3" y="3" width="8" height="8" rx="2" /><rect x="13" y="3" width="8" height="8" rx="2" /><rect x="3" y="13" width="8" height="8" rx="2" /><path d="m14 17 3 3 4-6" /></> :
        <><circle cx="10" cy="10" r="6" /><path d="m15 15 6 6M7 10h6M10 7v6" /></>}
    </svg></span><span>{label}</span>
  </Link>
}
