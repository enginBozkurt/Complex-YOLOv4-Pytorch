"""
# -*- coding: utf-8 -*-
-----------------------------------------------------------------------------------
# Refer: https://github.com/Tianxiaomo/pytorch-YOLOv4
# Refer: https://github.com/ghimiredhikura/Complex-YOLOv3
"""

import sys

import torch
import torch.nn as nn

sys.path.append('../')

from utils.torch_utils import to_cpu
from utils.evaluation_utils import rotated_box_wh_iou_polygon


class YoloLayer(nn.Module):
    """Yolo layer"""

    def __init__(self, num_classes, anchors, stride, scale_x_y, ignore_thresh):
        super(YoloLayer, self).__init__()
        # Update the attributions when parsing the cfg during create the darknet
        self.num_classes = num_classes
        self.anchors = anchors
        self.num_anchors = len(anchors)
        self.stride = stride
        self.scale_x_y = scale_x_y
        self.ignore_thresh = ignore_thresh

        self.coord_scale = 1
        self.noobj_scale = 1
        self.obj_scale = 5
        self.class_scale = 1

        self.seen = 0
        self.mse_loss = nn.MSELoss()
        self.bce_loss = nn.BCELoss()
        # Initialize dummy variables
        self.grid_size = 0
        self.img_size = 0
        self.metrics = {}

    def compute_grid_offsets(self, grid_size):
        self.grid_size = grid_size
        g = self.grid_size
        self.stride = self.img_size / self.grid_size
        # Calculate offsets for each grid
        self.grid_x = torch.arange(g, device=self.device, dtype=torch.float).repeat(g, 1).view([1, 1, g, g])
        self.grid_y = torch.arange(g, device=self.device, dtype=torch.float).repeat(g, 1).t().view([1, 1, g, g])
        self.scaled_anchors = torch.tensor(
            [(a_w / self.stride, a_h / self.stride, im, re) for a_w, a_h, im, re in self.anchors], device=self.device,
            dtype=torch.float)
        self.anchor_w = self.scaled_anchors[:, 0:1].view((1, self.num_anchors, 1, 1))
        self.anchor_h = self.scaled_anchors[:, 1:2].view((1, self.num_anchors, 1, 1))

    def build_targets(self, pred_cls, target, anchors):
        """ Built yolo targets to compute loss

        :param pred_cls: [num_samples or batch, num_anchors, grid_size, grid_size, num_classes]
        :param target: [num_boxes, 8]
        :param anchors: [num_anchors, 4]
        :return:
        """
        nB, nA, nG, _, nC = pred_cls.size()
        n_target_boxes = target.size(0)

        # Create output tensors on "device"
        obj_mask = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.uint8)
        noobj_mask = torch.full(size=(nB, nA, nG, nG), fill_value=1, device=self.device, dtype=torch.uint8)
        tx = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        ty = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tw = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        th = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tim = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tre = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tcls = torch.full(size=(nB, nA, nG, nG, nC), fill_value=0, device=self.device, dtype=torch.float)
        tconf = obj_mask.float()

        if n_target_boxes > 0:  # Make sure that there is at least 1 box
            # Convert to position relative to box
            target_boxes = target[:, 2:8]

            gxy = target_boxes[:, :2] * nG
            gwh = target_boxes[:, 2:4] * nG
            gimre = target_boxes[:, 4:]

            # Get anchors with best iou
            ious = torch.stack([rotated_box_wh_iou_polygon(anchor, gwh, gimre) for anchor in anchors])
            best_ious, best_n = ious.max(0)

            b, target_labels = target[:, :2].long().t()

            gx, gy = gxy.t()
            gw, gh = gwh.t()
            gim, gre = gimre.t()
            gi, gj = gxy.long().t()
            # Set masks
            obj_mask[b, best_n, gj, gi] = 1
            noobj_mask[b, best_n, gj, gi] = 0

            # Set noobj mask to zero where iou exceeds ignore threshold
            for i, anchor_ious in enumerate(ious.t()):
                noobj_mask[b[i], anchor_ious > self.ignore_thresh, gj[i], gi[i]] = 0

            # Coordinates
            tx[b, best_n, gj, gi] = gx - gx.floor()
            ty[b, best_n, gj, gi] = gy - gy.floor()
            # Width and height
            tw[b, best_n, gj, gi] = torch.log(gw / anchors[best_n][:, 0] + 1e-16)
            th[b, best_n, gj, gi] = torch.log(gh / anchors[best_n][:, 1] + 1e-16)
            # Im and real part
            tim[b, best_n, gj, gi] = gim
            tre[b, best_n, gj, gi] = gre

            # One-hot encoding of label
            tcls[b, best_n, gj, gi, target_labels] = 1
            tconf = obj_mask.float()

        return obj_mask.type(torch.bool), noobj_mask.type(torch.bool), tx, ty, tw, th, tim, tre, tcls, tconf

    def forward(self, x, targets=None, img_size=608):
        """
        :param x: [num_samples or batch, num_anchors * (6 + 1 + num_classes), grid_size, grid_size]
        :param targets: [num boxes, 8] (box_idx, class, x, y, w, l, sin(yaw), cos(yaw))
        :param img_size: default 608
        :return:
        """
        self.img_size = img_size
        self.device = x.device
        num_samples, _, _, grid_size = x.size()

        prediction = x.view(num_samples, self.num_anchors, self.num_classes + 7, grid_size, grid_size)
        prediction = prediction.permute(0, 1, 3, 4, 2).contiguous()
        # prediction size: [num_samples, num_anchors, grid_size, grid_size, num_classes + 7]

        # Get outputs
        x = torch.sigmoid(prediction[..., 0]) * self.scale_x_y - 0.5 * (self.scale_x_y - 1)  # Center x
        y = torch.sigmoid(prediction[..., 1]) * self.scale_x_y - 0.5 * (self.scale_x_y - 1)  # Center y
        w = prediction[..., 2]  # Width
        h = prediction[..., 3]  # Height
        im = prediction[..., 4]  # angle imaginary part
        re = prediction[..., 5]  # angle real part
        pred_conf = torch.sigmoid(prediction[..., 6])  # Conf
        pred_cls = torch.sigmoid(prediction[..., 7:])  # Cls pred.

        # If grid size does not match current we compute new offsets
        if grid_size != self.grid_size:
            self.compute_grid_offsets(grid_size)

        # Add offset and scale with anchors
        # pred_boxes size: [num_samples, num_anchors, grid_size, grid_size, 6]
        pred_boxes = torch.empty(prediction[..., :6].shape, device=self.device, dtype=torch.float)
        pred_boxes[..., 0] = x.detach() + self.grid_x
        pred_boxes[..., 1] = y.detach() + self.grid_y
        pred_boxes[..., 2] = torch.exp(w.detach()) * self.anchor_w
        pred_boxes[..., 3] = torch.exp(h.detach()) * self.anchor_h
        pred_boxes[..., 4] = im.detach()
        pred_boxes[..., 5] = re.detach()

        output = torch.cat((
            pred_boxes[..., :4].view(num_samples, -1, 4) * self.stride,
            pred_boxes[..., 4:].view(num_samples, -1, 2),
            pred_conf.view(num_samples, -1, 1),
            pred_cls.view(num_samples, -1, self.num_classes),
        ), dim=-1)
        # output size: [num_samples, num boxes, 7 + num_classes]

        if targets is None:
            return output, 0
        else:
            obj_mask, noobj_mask, tx, ty, tw, th, tim, tre, tcls, tconf = self.build_targets(pred_cls=pred_cls,
                                                                                             target=targets,
                                                                                             anchors=self.scaled_anchors)

            # Loss : Mask outputs to ignore non-existing objects (except with conf. loss)
            loss_x = self.mse_loss(x[obj_mask], tx[obj_mask])
            loss_y = self.mse_loss(y[obj_mask], ty[obj_mask])
            loss_w = self.mse_loss(w[obj_mask], tw[obj_mask])
            loss_h = self.mse_loss(h[obj_mask], th[obj_mask])
            loss_im = self.mse_loss(im[obj_mask], tim[obj_mask])
            loss_re = self.mse_loss(re[obj_mask], tre[obj_mask])
            loss_eular = loss_im + loss_re
            loss_conf_obj = self.bce_loss(pred_conf[obj_mask], tconf[obj_mask])
            loss_conf_noobj = self.bce_loss(pred_conf[noobj_mask], tconf[noobj_mask])
            loss_conf = self.obj_scale * loss_conf_obj + self.noobj_scale * loss_conf_noobj
            loss_cls = self.bce_loss(pred_cls[obj_mask], tcls[obj_mask])
            total_loss = loss_x + loss_y + loss_w + loss_h + loss_eular + loss_conf + loss_cls

            # Metrics (store loss values using tensorboard)
            self.metrics = {
                "loss": to_cpu(total_loss).item(),
                "x": to_cpu(loss_x).item(),
                "y": to_cpu(loss_y).item(),
                "w": to_cpu(loss_w).item(),
                "h": to_cpu(loss_h).item(),
                "im": to_cpu(loss_im).item(),
                "re": to_cpu(loss_re).item(),
                "conf": to_cpu(loss_conf).item(),
                "cls": to_cpu(loss_cls).item()
            }

            return output, total_loss
