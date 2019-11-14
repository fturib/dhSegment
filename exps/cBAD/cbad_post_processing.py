from skimage.measure import label as skimage_label
from typing import Tuple, List
from scipy.signal import convolve2d
from skimage.graph import MCP_Connect
from skimage.morphology import skeletonize
from sklearn.metrics.pairwise import euclidean_distances
from collections import defaultdict
import numpy as np
from scipy.ndimage import label

import cv2
import os
from doc_seg.utils import dump_pickle


def cbad_post_processing_fn(probs: np.array, sigma: float=2.5, low_threshold: float=0.8, high_threshold: float=0.9,
                            filter_width: float=0, output_basename=None):
    """

    :param probs: output of the model (probabilities) in range [0, 255]
    :param filename: filename of the image processed
    :param xml_output_dir: directory to export the resulting PAGE XML
    :param upsampled_shape: shape of the original image
    :param sigma:
    :param low_threshold:
    :param high_threshold:
    :return: contours, mask
     WARNING : contours IN OPENCV format List[np.ndarray(n_points, 1, (x,y))]
    """

    contours, lines_mask = line_extraction_v1(probs[:, :, 1], sigma, low_threshold, high_threshold, filter_width)
    if output_basename is not None:
        dump_pickle(output_basename+'.pkl', (contours, lines_mask.shape))
    return contours, lines_mask


def line_extraction_v0(probs, sigma, threshold):
    # probs_line = probs[:, :, 1]
    probs_line = probs
    # Smooth
    probs2 = cv2.GaussianBlur(probs_line, (int(3*sigma)*2+1, int(3*sigma)*2+1), sigma)

    lines_mask = probs2 >= threshold
    # Extract polygons from line mask
    contours = extract_line_polygons(lines_mask)

    return contours, lines_mask


def line_extraction_v1(probs, low_threshold, high_threshold, sigma=0.0, filter_width=0.00, vertical_maxima=False):
    probs_line = probs
    # Smooth
    if sigma > 0.:
        probs2 = cv2.GaussianBlur(probs_line, (int(3*sigma)*2+1, int(3*sigma)*2+1), sigma)
    else:
        probs2 = cv2.fastNlMeansDenoising((probs_line*255).astype(np.uint8), h=20)/255
    #probs2 = probs_line
    #local_maxima = vertical_local_maxima(probs2)
    lines_mask = hysteresis_thresholding(probs2, low_threshold, high_threshold,
                                         candidates=vertical_local_maxima(probs2) if vertical_maxima else None)
    # Remove lines touching border
    #lines_mask = remove_borders(lines_mask)
    # Extract polygons from line mask
    contours = extract_line_polygons(lines_mask)

    filtered_contours = []
    page_width = probs.shape[1]
    for cnt in contours:
        centroid_x, centroid_y = np.mean(cnt, axis=0)[0]
        if centroid_x < filter_width*page_width or centroid_x > (1-filter_width)*page_width:
            continue
        # if cv2.arcLength(cnt, False) < filter_width*page_width:
        #    continue
        filtered_contours.append(cnt)

    return filtered_contours, lines_mask


def line_extraction_v2(probs, sigma, low_threshold, high_threshold):
    probs_line = probs
    # Smooth
    probs2 = cv2.GaussianBlur(probs_line, (int(3*sigma)*2+1, int(3*sigma)*2+1), sigma)
    seeds = probs2 > high_threshold
    labelled_components, nb_components = label(seeds)

    lines_mask = hysteresis_thresholding(probs2, local_maxima, low_threshold, high_threshold)
    # Remove lines touching border
    #lines_mask = remove_borders(lines_mask)
    # Extract polygons from line mask
    contours = extract_line_polygons(lines_mask)

    filtered_contours = []
    page_width = probs.shape[1]
    for cnt in contours:
        if cv2.arcLength(cnt, False) < 0.05*page_width:
            continue
        if cv2.arcLength(cnt, False) < 0.05*page_width:
            continue
        filtered_contours.append(cnt)

    return filtered_contours, lines_mask


def extract_line_polygons(lines_mask):
    # Make sure one-pixel wide 8-connected mask
    lines_mask = skeletonize(lines_mask)

    class MakeLineMCP(MCP_Connect):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.connections = dict()
            self.scores = defaultdict(lambda: np.inf)

        def create_connection(self, id1, id2, pos1, pos2, cost1, cost2):
            k = (min(id1, id2), max(id1, id2))
            s = cost1 + cost2
            if self.scores[k] > s:
                self.connections[k] = (pos1, pos2, s)
                self.scores[k] = s

        def get_connections(self, subsample=5):
            results = dict()
            for k, (pos1, pos2, s) in self.connections.items():
                path = np.concatenate([self.traceback(pos1), self.traceback(pos2)[::-1]])
                results[k] = path[::subsample]
            return results

        def goal_reached(self, int_index, float_cumcost):
            if float_cumcost > 0:
                return 2
            else:
                return 0

    if np.sum(lines_mask) == 0:
        return []
    # Find extremities points
    end_points_candidates = np.stack(np.where((convolve2d(lines_mask, np.ones((3, 3)), mode='same') == 2) & lines_mask)).T
    connected_components = skimage_label(lines_mask, connectivity=2)
    # Group endpoint by connected components and keep only the two points furthest away
    d = defaultdict(list)
    for pt in end_points_candidates:
        d[connected_components[pt[0], pt[1]]].append(pt)
    end_points = []
    for pts in d.values():
        d = euclidean_distances(np.stack(pts), np.stack(pts))
        i, j = np.unravel_index(d.argmax(), d.shape)
        end_points.append(pts[i])
        end_points.append(pts[j])
    end_points = np.stack(end_points)

    mcp = MakeLineMCP(~lines_mask)
    mcp.find_costs(end_points)
    connections = mcp.get_connections()
    if not np.all(np.array(sorted([i for k in connections.keys() for i in k])) == np.arange(len(end_points))):
        print('Warning : extract_line_polygons seems weird')
    return [c[:, None, ::-1] for c in connections.values()]


def vertical_local_maxima(probs):
    local_maxima = np.zeros_like(probs, dtype=bool)
    local_maxima[1:-1] = (probs[1:-1] >= probs[:-2]) & (probs[2:] <= probs[1:-1])
    local_maxima = cv2.morphologyEx(local_maxima.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8))
    return local_maxima > 0


def hysteresis_thresholding(probs: np.array, low_threshold: float, high_threshold: float, candidates=None):
    low_mask = probs > low_threshold
    if candidates is not None:
        low_mask = candidates & low_mask
    # Connected components extraction
    label_components, count = label(low_mask, np.ones((3, 3)))
    # Keep components with high threshold elements
    good_labels = np.unique(label_components[low_mask & (probs > high_threshold)])
    label_masks = np.zeros((count + 1,), bool)
    label_masks[good_labels] = 1
    return label_masks[label_components]


def upscale_coordinates(list_points: List[np.array], ratio: Tuple[float, float]):  # list of (N,1,2) cv2 points
    return np.array(
        [(round(p[0, 0]*ratio[1]), round(p[0, 1]*ratio[0])) for p in list_points]
    )[:, None, :].astype(int)


def get_image_basename(image_filename: str, with_acronym: bool=False):
    # Get acronym followed by name of file
    directory, basename = os.path.split(image_filename)
    if with_acronym:
        acronym = directory.split(os.path.sep)[-1].split('_')[0]
        return '{}_{}'.format(acronym, basename.split('.')[0])
    else:
        return '{}'.format(basename.split('.')[0])


def get_page_filename(image_filename):
    return os.path.join(os.path.dirname(image_filename), 'page', '{}.xml'.format(os.path.basename(image_filename)[:-4]))


def remove_borders(mask, margin=5):
    tmp = mask.copy()
    tmp[:margin] = 1
    tmp[-margin:] = 1
    tmp[:, :margin] = 1
    tmp[:, -margin:] = 1
    label_components, count = label(tmp, np.ones((3, 3)))
    result = mask.copy()
    border_component = label_components[0, 0]
    result[label_components == border_component] = 0
    return result