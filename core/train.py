from core.helper_functions import *

import os
os.environ["PATH"] += os.pathsep + 'C:/Anaconda3/Library/bin/graphviz/'

from tensorflow.python.client import device_lib
import keras
from keras.optimizers import *
import tensorflow as tf
from keras.models import load_model

from core.augmentations.online_augment import OnlineAugmenter
from core.augmentations.offline_augment import OfflineAugmenter

import random
import time

import pickle

if Settings().USE_SE2:
    from core.architectures.se2unet import UNet
else:
    from core.architectures.unet import UNet

from keras.callbacks import TensorBoard


def write_log(callback, names, logs, batch_no):
    for name, value in zip(names, logs):
        summary = tf.Summary()
        summary_value = summary.value.add()
        summary_value.simple_value = value
        summary_value.tag = name
        callback.writer.add_summary(summary, batch_no)
        callback.writer.flush()


class Train:
    def __init__(self, s, h):
        self.sliceInformation = {}
        self.s = s
        self.h = h
        self.offline_augmenter = OfflineAugmenter(s, h)
        self.online_augmenter = OnlineAugmenter(s, h)

    def buildUNet(self):
        """
        Import Unet from unet_3D
        :return: model from Unet
        """
        ps = self.s.PATCH_SIZE
        if self.s.VARIABLE_PATCH_SIZE:
            ps = (None, None, None)
        if self.s.NR_DIM == 2:
            ps = ps[1:]
        if self.s.USE_LA_INPUT:
            ps += (2, )
        else:
            ps += (1, )
        model = UNet(ps, self.s.NR_DIM, dropout=self.s.DROPOUT, batchnorm=True, depth=self.s.UNET_DEPTH,
                     doeverylevel=self.s.DROPOUT_AT_EVERY_LEVEL, inc_rate=self.s.FEATURE_MAP_INC_RATE,
                     aux_loss=self.s.USE_LA_AUX_LOSS, nr_conv_per_block=self.s.NR_CONV_PER_CONV_BLOCK,
                     start_ch=self.s.START_CH, n_theta=self.s.SE2_N_THETA)

        if self.s.USE_LA_AUX_LOSS:
            model.compile(optimizer=Adam(lr=self.s.LEARNING_RATE), loss={'main_output': self.h.custom_loss,
                                                                         'aux_output': self.h.custom_loss},
                          metrics=['binary_accuracy'], loss_weights={'main_output': self.s.MAIN_OUTPUT_LOSS_WEIGHT,
                                                                     'aux_output': self.s.AUX_OUTPUT_LOSS_WEIGHT})
        else:
            model.compile(optimizer=Adam(lr=self.s.LEARNING_RATE), loss=self.h.custom_loss,
                          metrics=['binary_accuracy'])

        return model

    def getRandomNegativePatch(self, x_full, y_full, set_idx):
        i = random.randint(0, len(x_full) - 1)

        lap = -1

        if self.s.AUGMENT_ONLINE:
            x = x_full[i]
            y = y_full[i]
        else:
            zl = self.sliceInformation[set_idx[i]].shape[0]

            first = True
            while first or self.sliceInformation[set_idx[i]][s_nr]:
                first = False
                s_nr = random.randint(0, zl - 1 - self.s.PATCH_SIZE[0])

            x, y, la, lap = self.offline_augmenter.offline_augment(set_idx[i], range(s_nr, s_nr + self.s.PATCH_SIZE[0]),
                                                                  True, get_lap=self.s.USE_LA_INPUT,
                                                                  resize=self.s.RESIZE_BEFORE_TRAIN)

            if self.s.GROUND_TRUTH == 'left_atrium':
                y = la

        corner = [0, 0, 0]

        for i in range(3):
            if x.shape[i] < self.s.PATCH_SIZE[i]:
                corner[i] = round((x.shape[i] - self.s.PATCH_SIZE[i]) / 2)
            else:
                corner[i] = random.randint(0, x.shape[i] - self.s.PATCH_SIZE[i])

        if self.s.AUGMENT_ONLINE:
            x, y = self.online_augmenter.augment(x[corner[0]:corner[0] + self.s.PATCH_SIZE[0]], y[corner[0]:corner[0]
                                                 + self.s.PATCH_SIZE[0]], False, None)
            corner[0] = 0

        # print("x.shape == {}".format(x.shape))
        # print("y.shape == {}".format(y.shape))

        if self.s.VARIABLE_PATCH_SIZE:
            x_patch, y_patch, lap_patch, la_patch = x, y, lap, la
        else:
            if self.s.PATCH_SIZE[1] <= x.shape[1]:
                x_patch = self.h.cropImage(x, corner, self.s.PATCH_SIZE)
                lap_patch = self.h.cropImage(lap, corner, self.s.PATCH_SIZE)
                la_patch = self.h.cropImage(la, corner, self.s.PATCH_SIZE)
                y_patch = self.h.cropImage(y, corner, self.s.PATCH_SIZE)
            else:
                x_patch = self.h.rescaleImage(x[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:])
                lap_patch = (
                        self.h.rescaleImage(lap[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:]) > 0
                ).astype(int)
                la_patch = (
                        self.h.rescaleImage(la[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:]) > 0
                ).astype(int)
                y_patch = (
                        self.h.rescaleImage(y[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:]) > 0
                ).astype(int)

            if np.sum(y_patch) > 0:
                print('RETRY NEGATIVE PATCH')
                x_patch, lap_patch, y_patch, la_patch = self.getRandomNegativePatch(x_full, y_full, set_idx)

        x_patch = self.h.pre_process(x_patch)

        return x_patch, lap_patch, y_patch, la_patch

    def getRandomPositiveImage(self, x_full, y_full, set_idx):
        i = random.randint(0, len(x_full) - 1)
        x_pos, y_pos = x_full[i], y_full[i]

        if np.sum(y_pos) == 0:
            x_pos, y_pos = self.getRandomPositiveImage(x_full, y_full, set_idx)

        return x_pos, y_pos

    def getRandomPositiveSlices(self, x_i, y_i):
        its = 0
        while its == 0 or nz_z + self.s.PATCH_SIZE[0] > x_i.shape[0]:
            its += 1
            nz = np.nonzero(y_i)
            nz_z = random.choice(nz[0])

        x_s = x_i[nz_z:nz_z + x_i.shape[0]]
        y_s = y_i[nz_z:nz_z + y_i.shape[0]]
        return x_s, y_s

    def getRandomPositivePatchAllSlices(self, x, lap, y, la):
        if np.sum(y) == 0:
            return 0, 0, 0, 0, False

        if self.s.VARIABLE_PATCH_SIZE:
            x_patch, lap_patch, y_patch, la_patch = x, lap, y, la
        else:
            nz = np.nonzero(y)

            nz_i = random.randint(0, nz[0].shape[0] - 1)
            nz_yx = (nz[1][nz_i], nz[2][nz_i])
            ranges = ([-self.s.PATCH_SIZE[1] + 1, 0], [-self.s.PATCH_SIZE[2] + 1, 0])

            for i in range(2):
                if nz_yx[i] - self.s.PATCH_SIZE[i + 1] < 0:
                    ranges[i][0] = -nz_yx[i]
                if nz_yx[i] + self.s.PATCH_SIZE[i + 1] > x.shape[i + 1]:
                    ranges[i][1] = -(self.s.PATCH_SIZE[i + 1] - (x.shape[i + 1] - nz_yx[i]))

            corner = [0, 0, 0]

            for i in range(1, 3):
                if x.shape[i] < self.s.PATCH_SIZE[i]:
                    corner[i] = round((x.shape[i] - self.s.PATCH_SIZE[i]) / 2)
                else:
                    corner[i] = nz_yx[i - 1] + random.randint(ranges[i - 1][0], ranges[i - 1][1])

            if self.s.PATCH_SIZE[1] <= x.shape[1]:
                x_patch = self.h.cropImage(x, corner, self.s.PATCH_SIZE)
                lap_patch = self.h.cropImage(lap, corner, self.s.PATCH_SIZE)
                la_patch = self.h.cropImage(la, corner, self.s.PATCH_SIZE)
                y_patch = self.h.cropImage(y, corner, self.s.PATCH_SIZE)
            else:
                x_patch = self.h.rescaleImage(x[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:])
                lap_patch = (
                        self.h.rescaleImage(lap[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:]) > 0
                ).astype(int)
                la_patch = (
                        self.h.rescaleImage(la[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:]) > 0
                ).astype(int)
                y_patch = (
                        self.h.rescaleImage(y[corner[0]:corner[0]+self.s.PATCH_SIZE[0]], self.s.PATCH_SIZE[1:]) > 0
                ).astype(int)

        # imshow3D(y_patch)
        # imshow3D(y[corner[0]:corner[0]+self.s.PATCH_SIZE[0]])

        return x_patch, lap_patch, y_patch, la_patch, True

    def getRandomPositiveSlicesOffline(self, set_idx):
        lap_s = -1
        la_s = -1

        if random.random() < self.s.ART_FRACTION and set(set_idx) != set(self.s.VALIDATION_SET):
            x_s_path, y_s_path = self.h.getRandomArtificialPositiveImagePath(False, set_idx)

            # print('x_pos_path == {}'.format(x_s_path))
            # print('y_pos_path == {}'.format(y_s_path))

            x_s = self.h.loadImages([x_s_path])[0]
            y_s = self.h.loadImages([y_s_path])[0]

            x_s = np.reshape(x_s, (1,) + x_s.shape)
            y_s = np.reshape(y_s, (1,) + y_s.shape)
        else:
            its = 0
            while its == 0 or s_nr + self.s.PATCH_SIZE[0] > self.sliceInformation[img_nr].shape[0]:
                its += 1
                img_nr = random.choice(set_idx)
                w = np.where(self.sliceInformation[img_nr])
                s_nr = np.random.choice(w[0])

            x_s, y_s, la_s, lap_s = self.offline_augmenter.offline_augment(img_nr, range(s_nr, s_nr +
                                                                                         self.s.PATCH_SIZE[0]), True,
                                                                           get_lap=self.s.USE_LA_INPUT,
                                                                           resize=self.s.RESIZE_BEFORE_TRAIN)
            x_s = self.h.pre_process(x_s)

            if self.s.GROUND_TRUTH == 'left_atrium':
                y_s = la_s

        return x_s, lap_s, y_s, la_s

    def getRandomPositivePatch(self, x_full, y_full, set_idx):
        lap_s = -1

        if self.s.AUGMENT_ONLINE:
            x_i, y_i = self.getRandomPositiveImage(x_full, y_full, set_idx)
            x_s, y_s = self.getRandomPositiveSlices(x_i, y_i)
            x_s, y_s = self.online_augmenter.augment(x_s, y_s, False, None)
            x_s = self.h.pre_process(x_s)
        else:
            x_s, lap_s, y_s, la_s = self.getRandomPositiveSlicesOffline(set_idx)

        # imshow3D(
        #     np.concatenate(
        #         (x_s, y_s * np.max(x_s)), axis=2
        #     )
        # )

        x_patch, lap_patch, y_patch, la_patch, found = self.getRandomPositivePatchAllSlices(x_s, lap_s, y_s, la_s)

        if not found:
            x_patch, lap_patch, y_patch, la_patch = self.getRandomPositivePatch(x_full, y_full, set_idx)

        return x_patch, lap_patch, y_patch, la_patch

    def getRandomPatches(self, x_full, y_full, nr, set_idx):
        x = []
        y = []
        lap = []
        la = []

        for j in range(nr):
            positive_patch = random.random() < self.s.POS_NEG_PATCH_PROP  # Whether batch should be positive

            if not positive_patch:
                x_j, lap_j, y_j, la_j = self.getRandomNegativePatch(x_full, y_full, set_idx)
            else:
                x_j, lap_j, y_j, la_j = self.getRandomPositivePatch(x_full, y_full, set_idx)

            if self.s.VARIABLE_PATCH_SIZE:
                x_j = self.h.resize_to_unet_shape(x_j, self.s.UNET_DEPTH)
                lap_j = self.h.resize_to_unet_shape(lap_j, self.s.UNET_DEPTH)
                y_j = self.h.resize_to_unet_shape(y_j, self.s.UNET_DEPTH)
                la_j = self.h.resize_to_unet_shape(la_j, self.s.UNET_DEPTH)

            # print("positive_patch == {}".format(positive_patch))
            # print("x_j.shape == {}".format(x_j.shape))
            # print("y_j.shape == {}".format(y_j.shape))
            # print("np.sum(y_j) == {}".format(np.sum(y_j)))
            # imshow3D(np.concatenate((x_j / np.max(x_j), y_j), axis=2))

            x.append(x_j)
            y.append(y_j)
            lap.append(lap_j)
            la.append(la_j)

        if self.s.VARIABLE_PATCH_SIZE:
            for i in range(len(x)):
                x[i] = np.reshape(x[i], x[i].shape + (1,))
                y[i] = np.reshape(y[i], y[i].shape + (1,))
                la[i] = np.reshape(la[i], la[i].shape + (1,))
                lap[i] = np.reshape(lap[i], lap[i].shape + (1,))
        else:
            x = np.array(x)
            y = np.array(y)
            lap = np.array(lap)
            la = np.array(la)

            # if self.s.RESIZE_BEFORE_TRAIN:
            # sitk.WriteImage(sitk.GetImageFromArray(x[:, 0, :, :]), 'xb.nrrd')
            # sitk.WriteImage(sitk.GetImageFromArray(y[:, 0, :, :]), 'yb.nrrd')
            # sitk.WriteImage(sitk.GetImageFromArray(lap[:, 0, :, :]), 'lapb.nrrd')
            # sitk.WriteImage(sitk.GetImageFromArray(la[:, 0, :, :]), 'lab.nrrd')

            # print('before')
            # print(np.unique(y))
            # x[:, 0, :, :] = self.h.rescaleImage(x[:, 0, :, :], self.s.RESIZE_BEFORE_TRAIN)
            # y[:, 0, :, :] = self.h.rescaleImage(y[:, 0, :, :], self.s.RESIZE_BEFORE_TRAIN)
            # # print('after')
            # # print(np.unique(y))
            # lap[:, 0, :, :] = self.h.rescaleImage(lap[:, 0, :, :], self.s.RESIZE_BEFORE_TRAIN)
            # la[:, 0, :, :] = self.h.rescaleImage(la[:, 0, :, :], self.s.RESIZE_BEFORE_TRAIN)

            # sitk.WriteImage(sitk.GetImageFromArray(x[:, 0, :, :]), 'x.nrrd')
            # sitk.WriteImage(sitk.GetImageFromArray(y[:, 0, :, :]), 'y.nrrd')
            # sitk.WriteImage(sitk.GetImageFromArray(lap[:, 0, :, :]), 'lap.nrrd')
            # sitk.WriteImage(sitk.GetImageFromArray(la[:, 0, :, :]), 'la.nrrd')

            sh = x.shape
            if self.s.NR_DIM == 2:
                sh = sh[:1] + sh[2:]

            x = np.reshape(x, sh + (1,))
            lap = np.reshape(lap, sh + (1,))
            la = np.reshape(la, sh + (1,))
            y = np.reshape(y, sh + (1, ))

            if self.s.USE_NORMALIZATION:
                x = self.h.normalize_multiple_list(x) if self.s.VARIABLE_PATCH_SIZE else \
                          self.h.normalize_multiple_ndarray(x)

            # print('np.sum(x) == {}'.format(np.sum(x)))
            # print('np.sum(lap) == {}'.format(np.sum(lap)))
            # print('np.sum(y) == {}'.format(np.sum(y)))

            if self.s.USE_LA_INPUT:
                x = np.concatenate((x, lap), axis=self.s.NR_DIM+1)

            # print(x.shape)
            # # print(x[0, :, :, 0].shape)
            # for i in range(x.shape[0]):
            #     plt.figure()
            #     plt.subplot(1, 3, 1)
            #     plt.imshow(x[i, :, :, 0])
            #
            #     plt.subplot(1, 3, 2)
            #     plt.imshow(x[i, :, :, 1])
            #
            #     plt.subplot(1, 3, 3)
            #     plt.imshow(y[i, :, :, 0])
            #
            #     plt.show()

            # print('x.shape == {}'.format(x.shape))
        return x, y, la

    def updateSliceInformation(self, y_all, set_idx):
        for i in range(len(set_idx)):
            self.sliceInformation[set_idx[i]] = []
            for z in range(y_all[i].shape[0]):
                pos = np.sum(y_all[i][z]) > 0
                self.sliceInformation[set_idx[i]].append(pos)
            self.sliceInformation[set_idx[i]] = np.array(self.sliceInformation[set_idx[i]])

    def get_aux(self, y):
        y_aux = np.array([int(np.sum(y[j]) > 0) for j in range(y.shape[0])])
        y_aux = np.reshape(y_aux, (y_aux.shape[0], 1))

        return y_aux

    def train(self):
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=1)
        sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))

        # self.s.FN_CLASS_WEIGHT = 100
        # model = self.buildUNet()
        # plot_model(model, to_file='model.png')
        # print(model.summary())

        self.h.s = self.s

        print(device_lib.list_local_devices())

        x_all_path, y_all_path = self.h.getImagePaths(self.s.ALL_NATURAL_SET, False)

        # Full images
        x_full_all = self.h.loadImages(x_all_path)
        y_full_all = self.h.loadImages(y_all_path)

        self.x_full_all = x_full_all
        self.y_full_all = y_full_all

        # Divide full images in training and validation
        # to_subtract = 1 if self.s.DATA_SET == 'original' else 0

        x_full_train = [x_full_all[i - 1] for i in self.s.TRAINING_SET]
        y_full_train = [y_full_all[i - 1] for i in self.s.TRAINING_SET]
        x_full_val = [x_full_all[i - 1] for i in self.s.VALIDATION_SET]
        y_full_val = [y_full_all[i - 1] for i in self.s.VALIDATION_SET]

        self.updateSliceInformation(y_full_train, self.s.TRAINING_SET)
        self.updateSliceInformation(y_full_val, self.s.VALIDATION_SET)

        if self.s.FN_CLASS_WEIGHT == 'auto' and self.s.LOSS_FUNCTION == 'weighted_binary_cross_entropy':
            self.s.USE_NORMALIZATION = False
            _, y_patches, la_patches = self.getRandomPatches(x_full_train + x_full_val, y_full_train + y_full_val,
                                                             self.s.AUTO_CLASS_WEIGHT_N, list(self.s.TRAINING_SET)
                                                             + list(self.s.VALIDATION_SET))
            # self.s.USE_NORMALIZATION = True
            self.s.FN_CLASS_WEIGHT = self.h.getClassWeightAuto(y_patches)
            self.h.s = self.s

        if self.s.LOAD_MODEL:
            keras.losses.custom_loss = self.h.custom_loss
            model = load_model(self.h.getModelPath(self.s.MODEL_NAME))
        else:
            model = self.buildUNet()
        print(model.summary())

        log = {'training': {}, 'validation': {}}

        for m in model.metrics_names:
            log['training'][m] = []
            log['validation'][m] = []

        # print(model.metrics_names)

        # log = {'training': {'loss': [], 'accuracy': []}, 'validation': {'loss': [], 'accuracy': []}}

        start_time = time.time()
        lowest_val_loss = float("inf")
        lowest_train_loss = float("inf")

        # copyfile('settings.py', self.h.getModelSettingsPath(self.s.MODEL_NAME))

        print("Start training...")

        log_path = self.h.getLogPath(self.s.MODEL_NAME)
        log['fn_class_weight'] = self.s.FN_CLASS_WEIGHT
        log['settings'] = self.s

        es_j = 0  # Counter for early stopping

        log['stopped_early'] = False
        print("self.s.EARLY_STOPPING == {}".format(self.s.EARLY_STOPPING))
        print("self.s.PATIENCE_ES == {}".format(self.s.PATIENCE_ES))

        tb_log_path = self.h.getTbLogFolder(self.s.MODEL_NAME)

        tb_callback = TensorBoard(tb_log_path)
        tb_callback.set_model(model)

        start_i = 0
        training_duration = 0
        if self.s.LOAD_MODEL:
            log = pickle.load(open(log_path, "rb"))

            if not self.s.RESET_VAL_LOSS:
                lowest_val_loss = log['lowest_val_loss']
            start_i = len(log['training'][model.metrics_names[0]])

            es_j = start_i - log['lowest_val_loss_i'] if not self.s.RESET_PATIENCE_ES else 0

            if 'training_duration' in log.keys():
                start_time = time.time() - log['training_duration']

        pickle.dump(log, open(log_path, "wb"))

        train_names = ['train_'+m for m in model.metrics_names]
        val_names = ['val_'+m for m in model.metrics_names]

        for i in range(start_i, self.s.NR_BATCHES):
            if self.s.EARLY_STOPPING and self.s.PATIENCE_ES <= es_j:
                print("Stopped early at iteration {}".format(i))
                log['stopped_early'] = True
                break
            es_j += 1

            validate_in_this_step = i % self.s.VALIDATE_EVERY_ITER == 0

            print('{}s passed. Starting getRandomPatches.'.format(round(time.time() - start_time)))
            x_train, y_train, la_train = self.getRandomPatches(x_full_train, y_full_train, self.s.BATCH_SIZE,
                                                               self.s.TRAINING_SET)
            print('{}s passed. Ended getRandomPatches.'.format(round(time.time() - start_time)))

            # sitk.WriteImage(sitk.GetImageFromArray(x_train[:, :, :, 0]), 'x.nii.gz')
            # sitk.WriteImage(sitk.GetImageFromArray(y_train[:, :, :, 0]), 'y.nii.gz')
            #
            # return

            y_train_all = {'main_output': y_train}

            if self.s.USE_LA_AUX_LOSS:
                y_train_all['aux_output'] = la_train

            train_loss = model.train_on_batch(x_train, y_train_all)
            write_log(tb_callback, train_names, train_loss, i)

            if validate_in_this_step:
                x_val, y_val, la_val = self.getRandomPatches(x_full_val, y_full_val, self.s.NR_VAL_PATCH_PER_ITER,
                                                     self.s.VALIDATION_SET)
                y_val_all = {'main_output': y_val}

                if self.s.USE_LA_AUX_LOSS:
                    y_val_all['aux_output'] = la_val
                if self.s.VARIABLE_PATCH_SIZE:
                    raise Exception('Still to be implemented')
                    val_losses = []
                    for j in range(len(y_val_all)):
                         val_losses.append(
                             model.test_on_batch(x_val[j], {'main_output': y_train_all['main_output'][j]})
                         )
                    val_loss = np.mean(val_losses)
                else:
                    val_loss = []
                    for j in range(0, self.s.NR_VAL_PATCH_PER_ITER, self.s.BATCH_SIZE_VAL):
                        x_val_j = x_val[j:j+self.s.BATCH_SIZE_VAL]
                        y_val_j = {}
                        for n in list(y_val_all.keys()):
                            y_val_j[n] = y_val_all[n][j:j+self.s.BATCH_SIZE_VAL]
                        val_loss_j = model.test_on_batch(x_val_j, y_val_j)
                        if j == 0:
                            for n in range(len(val_loss_j)):
                                val_loss.append([val_loss_j[n]])
                        else:
                            for n in range(len(val_loss_j)):
                                # print('val_loss[n] == {}'.format(val_loss[n]))
                                val_loss[n].append(val_loss_j[n])
                        # print('val_loss == {}'.format(val_loss))
                    for n in range(len(val_loss_j)):
                        val_loss[n] = np.mean(val_loss[n])
                    # print('mean val_loss == {}'.format(val_loss))
                write_log(tb_callback, val_names, val_loss, i)

                loss_i = model.metrics_names.index('loss')
                if lowest_val_loss > val_loss[loss_i]:
                    lowest_val_loss = val_loss[loss_i]
                    model_path = self.h.getModelPath(self.s.MODEL_NAME)
                    model.save(model_path)
                    lowest_val_loss_i = i
                    log['lowest_val_loss'] = lowest_val_loss
                    log['lowest_val_loss_i'] = lowest_val_loss_i
                    es_j = 0
                    lowest_train_loss = min(log['training']['loss']) if len(log['training']['loss']) > 0 else float("inf")

            for m in range(len(model.metrics_names)):
                log['training'][model.metrics_names[m]].append(train_loss[m])
                if validate_in_this_step:
                    log['validation'][model.metrics_names[m]].append(val_loss[m])
                else:
                    val_loss = ['none']

            if lowest_train_loss > train_loss[0]:
                lowest_train_loss = train_loss[0]

            ETA = round(time.time() - start_time) * (1/((i + 1) / self.s.NR_BATCHES) - 1)

            training_duration = round(time.time() - start_time)
            log['training_duration'] = training_duration
            print(('{}s passed. ETA is {}s. Finished training on batch {}/{} ({}%). Latest, lowest validation loss:' +
                  ' {}, {}. Latest, lowest training loss: {}, {}.').format(
                training_duration, ETA, i + 1, self.s.NR_BATCHES, (i + 1) /
                      self.s.NR_BATCHES * 100, val_loss[0],
                lowest_val_loss, train_loss[0], lowest_train_loss))

            pickle.dump(log, open(log_path, "wb"))

        print('Training took {} seconds.'.format(training_duration))

        pickle.dump(log, open(log_path, "wb"))

        del sess


if __name__ == "__main__":
    s = Settings()
    h = Helper(s)
    t = Train(s, h)
    t.train()
