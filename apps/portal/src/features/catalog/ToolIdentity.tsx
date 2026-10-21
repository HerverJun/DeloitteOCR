import { Link } from 'react-router-dom'
import AppstoreOutlined from '@ant-design/icons/es/icons/AppstoreOutlined'
import AudioOutlined from '@ant-design/icons/es/icons/AudioOutlined'
import ScanOutlined from '@ant-design/icons/es/icons/ScanOutlined'
import SearchOutlined from '@ant-design/icons/es/icons/SearchOutlined'
import TableOutlined from '@ant-design/icons/es/icons/TableOutlined'
import UserSwitchOutlined from '@ant-design/icons/es/icons/UserSwitchOutlined'

export function ToolGlyph({ appId }: { appId: string }) {
  const Icon = appId === 'ocr' ? ScanOutlined : appId === 'classification' ? AppstoreOutlined : appId === 'data' ? TableOutlined
    : appId === 'face' ? UserSwitchOutlined : appId === 'audio' ? AudioOutlined : SearchOutlined
  return <Icon aria-hidden="true" />
}

export function ToolIdentity({ appId, to, label }: { appId: string; to: string; label: string }) {
  return <Link to={to} className="application-identity">
    <span className="application-icon" aria-hidden="true"><ToolGlyph appId={appId} /></span><span>{label}</span>
  </Link>
}
