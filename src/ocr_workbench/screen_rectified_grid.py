"""Read-only perspective grid proof, recomputed entirely from original pixels.

Prepared PNGs are never trusted or needed. A unique recorded transform must be
invertible, remain inside the source and bind a single complete observed grid to
the single business region. Original candidate values and review state survive.
"""
from copy import deepcopy
import hashlib

from ocr_workbench.coordinates import bounds, box_polygon, inverse, matrix, polygon, validate_polygon


def observe_rectified_grid(raw, info, evidence, direct):
    from ocr_workbench.screen_diagnostics import (
        _area, _digest, _grid_source, _intersection, _observe_grid_cells, _regions, observed_box)

    result=deepcopy(direct)
    attempt={'status':'unavailable','reason':None}
    result['rectified_attempt']=attempt
    tables=raw.get('tables',[]) or []
    regions=_regions(info)
    candidates=[part for part in info.get('prepared_images',[]) if isinstance(part,dict) and part.get('kind')=='perspective']
    if len(tables)!=1 or len(regions)!=1 or len(candidates)!=1:
        attempt['reason']='no_unique_perspective_table_region'
        return result
    image,reason=_grid_source(raw,info,evidence)
    if reason:
        attempt['reason']=reason
        return result
    try:
        import cv2
        import numpy as np
        from PIL import Image
        from ocr_workbench.screen_imaging import _line_regions

        candidate=candidates[0]
        width,height=candidate.get('width'),candidate.get('height')
        if any(type(value) is not int or not 2<=value<32767 for value in (width,height)) or width*height>24000000:
            attempt['reason']='rectification_work_limit'
            return result
        transform=matrix(candidate.get('source_to_prepared'))
        recorded_inverse=matrix(candidate.get('prepared_to_source'))
        inverted=inverse(transform)
        if not np.allclose(inverted,recorded_inverse,rtol=1e-9,atol=1e-7):
            attempt['reason']='rectification_inverse_disagrees'
            return result
        corners=box_polygon([0,0,width,height])
        # A projective horizon through the rectangle invalidates its interior.
        denominators=[inverted[6]*x+inverted[7]*y+inverted[8] for x,y in corners]
        if not (min(denominators)>1e-9 or max(denominators)<-1e-9):
            attempt['reason']='rectification_crosses_projective_horizon'
            return result
        source_polygon=polygon(inverted,corners)
        validate_polygon(source_polygon,image.width,image.height)
        if not cv2.isContourConvex(np.asarray(source_polygon,dtype=np.float32)):
            attempt['reason']='rectification_source_not_convex'
            return result
        if candidate.get('source_polygon') is None or not np.allclose(
                np.asarray(candidate['source_polygon']),np.asarray(source_polygon),rtol=1e-9,atol=1e-7):
            attempt['reason']='rectification_source_polygon_disagrees'
            return result
        rectified=Image.fromarray(cv2.warpPerspective(np.asarray(image),np.asarray(transform).reshape(3,3),
            (width,height),flags=cv2.INTER_CUBIC,borderValue=(255,255,255)))
        matching=[]
        for region in _line_regions(rectified):
            source_box=bounds(polygon(inverted,box_polygon(region['box'])))
            if _intersection(source_box,regions[0]['table_box'])/max(_area(source_box),_area(regions[0]['table_box']))>=.8:
                matching.append(region)
        if len(matching)!=1:
            attempt['reason']='no_unique_complete_rectified_grid'
            return result
        region={**matching[0],'id':regions[0].get('id'),'table_box':matching[0]['box']}
        rectified_raw=deepcopy(raw)
        for table in rectified_raw['tables']:
            for item in [table,*table.get('cells',[])]:
                box=observed_box(item)
                if box:
                    item['polygon']=polygon(transform,box_polygon(box))
                    item['box']=bounds(item['polygon'])
        mapped=deepcopy(evidence)
        readings=mapped.get('text',mapped.get('blocks',[])) if isinstance(mapped,dict) else mapped
        for item in readings or []:
            if box:=observed_box(item):
                item['polygon']=polygon(transform,box_polygon(box))
                item['box']=bounds(item['polygon'])
        rectified_info={**info,'width':width,'height':height,'business_regions':[region]}
        proof=_observe_grid_cells(rectified_raw,rectified_info,mapped,source=(rectified,None))
        attempt.update(status='verified' if proof['cell_count'] else 'unavailable',
            reason=None if proof['cell_count'] else 'rectified_partition_or_anchors_unverified',
            plane_proof=proof)
        if not proof['cell_count']:return result
        row=deepcopy(proof['tables'][0])
        for cell in row['cells']:
            cell['rectified_box']=cell['box']
            cell['rectified_polygon']=cell['polygon']
            cell['polygon']=polygon(inverted,cell['rectified_polygon'])
            validate_polygon(cell['polygon'],image.width,image.height)
            cell['box']=bounds(cell['polygon'])
            cell.update(geometry_origin='source_recomputed_rectified_grid_with_text_anchors',
                basis='source_pixels_rectified_partition_independent_anchors_inverse_projection')
        row['cell_boxes']={':'.join(str(cell[key]) for key in ('row','column','row_span','column_span')):
            cell['box'] for cell in row['cells']}
        row['coordinate_space']='exif_normalized_original'
        row['rectification']={'version':'source-recomputed-rectified-grid-v1',
            'source_sha256':info['input_sha256'],'source_size':list(image.size),
            'prepared_size':[width,height],'source_to_prepared':transform,'prepared_to_source':inverted,
            'source_polygon':source_polygon,
            # The prepared file is never read. Its relocated storage address
            # cannot change a proof recomputed from the same original pixels.
            'transform_snapshot_sha256':_digest({key:value for key,value in candidate.items() if key!='path'}),
            'transform_snapshot_basis':'recorded_transform_fields_without_prepared_file_path_v1',
            'recomputed_rgb_sha256':hashlib.sha256(rectified.tobytes()).hexdigest(),
            'recomputed_mode':'RGB','interpolation':'cv2.INTER_CUBIC',
            'prepared_file_read':False,'pixel_grid_redetected':True,
            'plane_binding':deepcopy(proof['binding'])}
        result.update(version='screen-grid-localization-v2',tables=[row],cell_count=proof['cell_count'],
            source_plane_attempt=deepcopy(direct['tables']))
        result['binding']['rectification_sha256']=_digest(row['rectification'])
        return result
    except ImportError:
        attempt.update(status='unavailable',reason='rectification_observer_unavailable')
    except (ValueError,TypeError,KeyError,OverflowError,IndexError):
        attempt.update(status='unavailable',reason='invalid_rectification_geometry')
    return result
