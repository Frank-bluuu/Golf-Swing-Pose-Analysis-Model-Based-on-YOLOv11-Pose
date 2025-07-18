# Ultralytics 🚀 AGPL-3.0 License - https://ultralytics.com/license

from pathlib import Path

import numpy as np
import torch

from ultralytics.models.yolo.detect import DetectionValidator
from ultralytics.utils import LOGGER, ops
from ultralytics.utils.checks import check_requirements
from ultralytics.utils.metrics import OKS_SIGMA, PoseMetrics, box_iou, kpt_iou, kpt_iou_club
from ultralytics.utils.plotting import output_to_target, plot_images


class PoseValidator(DetectionValidator):
    """
    A class extending the DetectionValidator class for validation based on a pose model.

    This validator is specifically designed for pose estimation tasks, handling keypoints and implementing
    specialized metrics for pose evaluation.

    Attributes:
        sigma (np.ndarray): Sigma values for OKS calculation, either from OKS_SIGMA or ones divided by number of keypoints.
        kpt_shape (List[int]): Shape of the keypoints, typically [17, 3] for COCO format.
        args (dict): Arguments for the validator including task set to "pose".
        metrics (PoseMetrics): Metrics object for pose evaluation.

    Methods:
        preprocess: Preprocesses batch data for pose validation.
        get_desc: Returns description of evaluation metrics.
        init_metrics: Initializes pose metrics for the model.
        _prepare_batch: Prepares a batch for processing.
        _prepare_pred: Prepares and scales predictions for evaluation.
        update_metrics: Updates metrics with new predictions.
        _process_batch: Processes batch to compute IoU between detections and ground truth.
        plot_val_samples: Plots validation samples with ground truth annotations.
        plot_predictions: Plots model predictions.
        save_one_txt: Saves detections to a text file.
        pred_to_json: Converts predictions to COCO JSON format.
        eval_json: Evaluates model using COCO JSON format.

    Examples:
        >>> from ultralytics.models.yolo.pose import PoseValidator
        >>> args = dict(model="yolo11n-pose.pt", data="coco8-pose.yaml")
        >>> validator = PoseValidator(args=args)
        >>> validator()
    """

    def __init__(self, dataloader=None, save_dir=None, pbar=None, args=None, _callbacks=None):
        """Initialize a PoseValidator object with custom parameters and assigned attributes."""
        super().__init__(dataloader, save_dir, pbar, args, _callbacks)
        self.sigma = None
        self.kpt_shape = None
        self.args.task = "pose"
        self.metrics = PoseMetrics(save_dir=self.save_dir)
        if isinstance(self.args.device, str) and self.args.device.lower() == "mps":
            LOGGER.warning(
                "WARNING ⚠️ Apple MPS known Pose bug. Recommend 'device=cpu' for Pose models. "
                "See https://github.com/ultralytics/ultralytics/issues/4031."
            )

    def preprocess(self, batch):
        """Preprocess batch by converting keypoints data to float and moving it to the device."""
        batch = super().preprocess(batch)
        batch["keypoints"] = batch["keypoints"].to(self.device).float()
        return batch

    def get_desc(self):
        """Return description of evaluation metrics in string format."""
        return ("%22s" + "%11s" * 14) % (
            "Class",
            "Images",
            "Instances",
            "Box(P",
            "R",
            "mAP50",
            "mAP50-95)",
            "Pose(P",
            "R",
            "mAP50",
            "mAP50-95)",
            "Pose_club(P",
            "R",
            "mAP50",
            "mAP50-95)",
        )

    def init_metrics(self, model):
        """Initialize pose estimation metrics for YOLO model."""
        super().init_metrics(model)
        self.kpt_shape = self.data["kpt_shape"]
        is_pose = self.kpt_shape == [19, 3] if OKS_SIGMA.size == 19 else self.kpt_shape == [17, 3]
        nkpt = self.kpt_shape[0]
        self.sigma = OKS_SIGMA if is_pose else np.ones(nkpt) / nkpt
        self.stats = dict(tp_p=[], tp=[], tp_p_club=[],conf=[], pred_cls=[], target_cls=[], target_img=[])

    def _prepare_batch(self, si, batch):
        """Prepare a batch for processing by converting keypoints to float and scaling to original dimensions."""
        pbatch = super()._prepare_batch(si, batch)
        kpts = batch["keypoints"][batch["batch_idx"] == si]
        h, w = pbatch["imgsz"]
        kpts = kpts.clone()
        kpts[..., 0] *= w
        kpts[..., 1] *= h
        kpts = ops.scale_coords(pbatch["imgsz"], kpts, pbatch["ori_shape"], ratio_pad=pbatch["ratio_pad"])
        pbatch["kpts"] = kpts
        return pbatch

    def _prepare_pred(self, pred, pbatch):
        """Prepare and scale keypoints in predictions for pose processing."""
        predn = super()._prepare_pred(pred, pbatch)
        nk = pbatch["kpts"].shape[1]
        pred_kpts = predn[:, 6:].view(len(predn), nk, -1)
        ops.scale_coords(pbatch["imgsz"], pred_kpts, pbatch["ori_shape"], ratio_pad=pbatch["ratio_pad"])
        return predn, pred_kpts

    def update_metrics(self, preds, batch):
        """
        Update metrics with new predictions and ground truth data.

        This method processes each prediction, compares it with ground truth, and updates various statistics
        for performance evaluation.

        Args:
            preds (List[torch.Tensor]): List of prediction tensors from the model.
            batch (dict): Batch data containing images and ground truth annotations.
        """
        for si, pred in enumerate(preds):
            self.seen += 1
            npr = len(pred)
            stat = dict(
                conf=torch.zeros(0, device=self.device),
                pred_cls=torch.zeros(0, device=self.device),
                tp=torch.zeros(npr, self.niou, dtype=torch.bool, device=self.device),
                tp_p=torch.zeros(npr, self.niou, dtype=torch.bool, device=self.device),
                tp_p_club=torch.zeros(npr, self.niou, dtype=torch.bool, device=self.device),
            )
            pbatch = self._prepare_batch(si, batch)
            cls, bbox = pbatch.pop("cls"), pbatch.pop("bbox")
            nl = len(cls)
            stat["target_cls"] = cls
            stat["target_img"] = cls.unique()
            if npr == 0:
                if nl:
                    for k in self.stats.keys():
                        self.stats[k].append(stat[k])
                    if self.args.plots:
                        self.confusion_matrix.process_batch(detections=None, gt_bboxes=bbox, gt_cls=cls)
                continue

            # Predictions
            if self.args.single_cls:
                pred[:, 5] = 0
            predn, pred_kpts = self._prepare_pred(pred, pbatch)
            stat["conf"] = predn[:, 4]
            stat["pred_cls"] = predn[:, 5]

            # Evaluate
            if nl:
                stat["tp"] = self._process_batch(predn, bbox, cls)
                stat["tp_p"] = self._process_batch(predn, bbox, cls, pred_kpts, pbatch["kpts"])
                stat["tp_p_club"] = self._process_batch_club(predn, bbox, cls, pred_kpts, pbatch["kpts"])

            count1=torch.sum(stat["tp"] ).item()
            count2=torch.sum(stat["tp_p"] ).item()
            count3=torch.sum(stat["tp_p_club"] ).item()


            if self.args.plots:
                self.confusion_matrix.process_batch(predn, bbox, cls)

            for k in self.stats.keys():
                self.stats[k].append(stat[k])

            # Save
            if self.args.save_json:
                self.pred_to_json(predn, batch["im_file"][si])
            if self.args.save_txt:
                self.save_one_txt(
                    predn,
                    pred_kpts,
                    self.args.save_conf,
                    pbatch["ori_shape"],
                    self.save_dir / "labels" / f"{Path(batch['im_file'][si]).stem}.txt",
                )

    def _process_batch(self, detections, gt_bboxes, gt_cls, pred_kpts=None, gt_kpts=None):
        """
        Return correct prediction matrix by computing Intersection over Union (IoU) between detections and ground truth.

        Args:
            detections (torch.Tensor): Tensor with shape (N, 6) representing detection boxes and scores, where each
                detection is of the format (x1, y1, x2, y2, conf, class).
            gt_bboxes (torch.Tensor): Tensor with shape (M, 4) representing ground truth bounding boxes, where each
                box is of the format (x1, y1, x2, y2).
            gt_cls (torch.Tensor): Tensor with shape (M,) representing ground truth class indices.
            pred_kpts (torch.Tensor | None): Optional tensor with shape (N, 51) representing predicted keypoints, where
                51 corresponds to 17 keypoints each having 3 values.
            gt_kpts (torch.Tensor | None): Optional tensor with shape (N, 51) representing ground truth keypoints.

        Returns:
            (torch.Tensor): A tensor with shape (N, 10) representing the correct prediction matrix for 10 IoU levels,
                where N is the number of detections.

        Notes:
            `0.53` scale factor used in area computation is referenced from
            https://github.com/jin-s13/xtcocoapi/blob/master/xtcocotools/cocoeval.py#L384.
        """
        if pred_kpts is not None and gt_kpts is not None:
            # `0.53` is from https://github.com/jin-s13/xtcocoapi/blob/master/xtcocotools/cocoeval.py#L384
            area = ops.xyxy2xywh(gt_bboxes)[:, 2:].prod(1) * 0.53
            iou = kpt_iou(gt_kpts, pred_kpts, sigma=self.sigma, area=area)
        else:  # boxes
            iou = box_iou(gt_bboxes, detections[:, :4])

        return self.match_predictions(detections[:, 5], gt_cls, iou)


    def _process_batch_club(self, detections, gt_bboxes, gt_cls, pred_kpts=None, gt_kpts=None):
        """ 处理球杆关键点的匹配 """
        if pred_kpts is not None and gt_kpts is not None:
            # `0.53` is from https://github.com/jin-s13/xtcocoapi/blob/master/xtcocotools/cocoeval.py#L384
            area = ops.xyxy2xywh(gt_bboxes)[:, 2:].prod(1) * 0.53
            iou = kpt_iou_club(gt_kpts, pred_kpts, sigma=self.sigma[11:13], area=area)
        else:
            iou = torch.zeros((len(gt_bboxes), len(detections)), device=self.device)

        return self.match_predictions(detections[:, 5], gt_cls, iou)


    def plot_val_samples(self, batch, ni):
        """Plot and save validation set samples with ground truth bounding boxes and keypoints."""
        plot_images(
            batch["img"],
            batch["batch_idx"],
            batch["cls"].squeeze(-1),
            batch["bboxes"],
            kpts=batch["keypoints"],
            paths=batch["im_file"],
            fname=self.save_dir / f"val_batch{ni}_labels.jpg",
            names=self.names,
            on_plot=self.on_plot,
        )

    def plot_predictions(self, batch, preds, ni):
        """Plot and save model predictions with bounding boxes and keypoints."""
        pred_kpts = torch.cat([p[:, 6:].view(-1, *self.kpt_shape) for p in preds], 0)
        plot_images(
            batch["img"],
            *output_to_target(preds, max_det=self.args.max_det),
            kpts=pred_kpts,
            paths=batch["im_file"],
            fname=self.save_dir / f"val_batch{ni}_pred.jpg",
            names=self.names,
            on_plot=self.on_plot,
        )  # pred

    def save_one_txt(self, predn, pred_kpts, save_conf, shape, file):
        """Save YOLO detections to a txt file in normalized coordinates in a specific format."""
        from ultralytics.engine.results import Results

        Results(
            np.zeros((shape[0], shape[1]), dtype=np.uint8),
            path=None,
            names=self.names,
            boxes=predn[:, :6],
            keypoints=pred_kpts,
        ).save_txt(file, save_conf=save_conf)

    def pred_to_json(self, predn, filename):
        """Convert YOLO predictions to COCO JSON format."""
        stem = Path(filename).stem
        image_id = int(stem) if stem.isnumeric() else stem
        box = ops.xyxy2xywh(predn[:, :4])  # xywh
        box[:, :2] -= box[:, 2:] / 2  # xy center to top-left corner
        for p, b in zip(predn.tolist(), box.tolist()):
            self.jdict.append(
                {
                    "image_id": image_id,
                    "category_id": self.class_map[int(p[5])],
                    "bbox": [round(x, 3) for x in b],
                    "keypoints": p[6:],
                    "score": round(p[4], 5),
                }
            )

    def eval_json(self, stats):
        """Evaluate object detection model using COCO JSON format."""
        if self.args.save_json and self.is_coco and len(self.jdict):
            anno_json = self.data["path"] / "annotations/person_keypoints_val2017.json"  # annotations
            pred_json = self.save_dir / "predictions.json"  # predictions
            LOGGER.info(f"\nEvaluating pycocotools mAP using {pred_json} and {anno_json}...")
            try:  # https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocoEvalDemo.ipynb
                check_requirements("pycocotools>=2.0.6")
                from pycocotools.coco import COCO  # noqa
                from pycocotools.cocoeval import COCOeval  # noqa

                for x in anno_json, pred_json:
                    assert x.is_file(), f"{x} file not found"
                anno = COCO(str(anno_json))  # init annotations api
                pred = anno.loadRes(str(pred_json))  # init predictions api (must pass string, not Path)
                for i, eval in enumerate([COCOeval(anno, pred, "bbox"), COCOeval(anno, pred, "keypoints")]):
                    if self.is_coco:
                        eval.params.imgIds = [int(Path(x).stem) for x in self.dataloader.dataset.im_files]  # im to eval
                    eval.evaluate()
                    eval.accumulate()
                    eval.summarize()
                    idx = i * 4 + 2
                    stats[self.metrics.keys[idx + 1]], stats[self.metrics.keys[idx]] = eval.stats[
                        :2
                    ]  # update mAP50-95 and mAP50
            except Exception as e:
                LOGGER.warning(f"pycocotools unable to run: {e}")
        return stats
