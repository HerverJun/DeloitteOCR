export type ImageSize = [number, number]
export type Matrix = [[number, number, number], [number, number, number], [number, number, number]]
export type NormalizedBox = [number, number, number, number]
export type ForensicGeometry = {
  schema_version: '1.0'; coordinate_version: 'exif-pixel-edges-v1'
  source_coordinate_system: 'stored_pixels'; display_coordinate_system: 'exif_transposed_pixels'
  stored_size: ImageSize; display_size: ImageSize; exif_orientation: number
  orientation_status: 'valid' | 'absent'; coordinate_convention: 'pixel_edges'
  stored_to_display: Matrix; display_to_stored: Matrix
  persisted_region_coordinate_system?: string; map_coordinate_system?: string
}

export function orientationMatrix(orientation: number, [width, height]: ImageSize): Matrix {
  const matrices: Matrix[] = [
    [[1, 0, 0], [0, 1, 0], [0, 0, 1]], [[-1, 0, width], [0, 1, 0], [0, 0, 1]],
    [[-1, 0, width], [0, -1, height], [0, 0, 1]], [[1, 0, 0], [0, -1, height], [0, 0, 1]],
    [[0, 1, 0], [1, 0, 0], [0, 0, 1]], [[0, -1, height], [1, 0, 0], [0, 0, 1]],
    [[0, -1, height], [-1, 0, width], [0, 0, 1]], [[0, 1, 0], [-1, 0, width], [0, 0, 1]],
  ]
  if (!Number.isInteger(orientation) || orientation < 1 || orientation > 8) throw new Error('Invalid EXIF orientation')
  return matrices[orientation - 1]
}

export function inverseMatrix(matrix: Matrix): Matrix {
  const [[a, b, c], [d, e, f]] = matrix, determinant = a * e - b * d
  if (!determinant) throw new Error('Invalid coordinate matrix')
  return [[e / determinant, -b / determinant, (b * f - e * c) / determinant],
    [-d / determinant, a / determinant, (d * c - a * f) / determinant], [0, 0, 1]]
}

export function imageSize(value: unknown): ImageSize | null {
  return Array.isArray(value) && value.length === 2 && value.every(number => Number.isSafeInteger(number) && number > 0)
    ? value as ImageSize : null
}

function sameMatrix(actual: unknown, expected: Matrix) {
  return Array.isArray(actual) && actual.length === 3 && actual.every((row, y) =>
    Array.isArray(row) && row.length === 3 && row.every((value, x) =>
      typeof value === 'number' && Number.isFinite(value) && Math.abs(value - expected[y][x]) < 1e-6))
}

/** Only the explicit versioned EXIF contract permits image registration. */
export function readGeometry(value: unknown): ForensicGeometry | null {
  if (!value || typeof value !== 'object') return null
  const geometry = value as ForensicGeometry
  const stored = imageSize(geometry.stored_size), display = imageSize(geometry.display_size)
  if (!stored || !display || geometry.schema_version !== '1.0' || geometry.coordinate_version !== 'exif-pixel-edges-v1'
    || geometry.source_coordinate_system !== 'stored_pixels' || geometry.display_coordinate_system !== 'exif_transposed_pixels'
    || geometry.coordinate_convention !== 'pixel_edges' || !['valid', 'absent'].includes(geometry.orientation_status)
    || !Number.isInteger(geometry.exif_orientation) || geometry.exif_orientation < 1 || geometry.exif_orientation > 8) return null
  if (geometry.orientation_status === 'absent' && geometry.exif_orientation !== 1) return null
  const expectedSize = geometry.exif_orientation >= 5 ? [stored[1], stored[0]] : stored
  if (!display.every((number, index) => number === expectedSize[index])) return null
  const forward = orientationMatrix(geometry.exif_orientation, stored)
  if (!sameMatrix(geometry.stored_to_display, forward) || !sameMatrix(geometry.display_to_stored, inverseMatrix(forward))) return null
  return geometry
}

/** Rectangles are transformed by all four pixel-edge corners, including mirrored EXIF orientations. */
export function transformBox(box: NormalizedBox, matrix: Matrix, from: ImageSize, to: ImageSize): NormalizedBox {
  const points = [[box[0], box[1]], [box[2], box[1]], [box[0], box[3]], [box[2], box[3]]].map(([x, y]) => {
    const px = x * from[0], py = y * from[1]
    return [(matrix[0][0] * px + matrix[0][1] * py + matrix[0][2]) / to[0],
      (matrix[1][0] * px + matrix[1][1] * py + matrix[1][2]) / to[1]]
  })
  const clamp = (value: number) => Math.max(0, Math.min(1, value))
  return [clamp(Math.min(...points.map(point => point[0]))), clamp(Math.min(...points.map(point => point[1]))),
    clamp(Math.max(...points.map(point => point[0]))), clamp(Math.max(...points.map(point => point[1])))]
}

export const storedBoxToDisplay = (box: NormalizedBox, geometry: ForensicGeometry) =>
  transformBox(box, geometry.stored_to_display, geometry.stored_size, geometry.display_size)
export const displayBoxToStored = (box: NormalizedBox, geometry: ForensicGeometry) =>
  transformBox(box, geometry.display_to_stored, geometry.display_size, geometry.stored_size)
