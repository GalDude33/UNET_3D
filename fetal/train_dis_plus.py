import glob
import os
from datetime import datetime
from pathlib import Path

import keras.backend as K
import nibabel as nib
import numpy as np
from keras import Input, Model
from keras.engine.network import Network
from keras.layers import Activation, Conv2D, Flatten, Dense, LeakyReLU
from keras.optimizers import Adam
from tqdm import tqdm

import fetal_net
import fetal_net.metrics
import fetal_net.preprocess
from fetal.config_utils import get_config
from fetal.utils import get_last_model_path, create_data_file, set_gpu_mem_growth, build_dsc, Scheduler
from fetal_net.data import open_data_file
from fetal_net.generator import get_training_and_validation_generators

set_gpu_mem_growth()

config = get_config()
if not "dis_model_name" in config:
    config["dis_model_name"] = "discriminator_image_3d"
if not "dis_loss" in config:
    config["dis_loss"] = "binary_crossentropy_loss"
if not "gen_steps" in config:
    config["gen_steps"] = 1
if not "dis_steps" in config:
    config["dis_steps"] = 1
if not "gdS_loss_ratio" in config:
    config["gdS_loss_ratio"] = 0.25
if not "gdE_loss_ratio" in config:
    config["gdE_loss_ratio"] = 0.25
if not "save_evals" in config:
    config["save_evals"] = 2
base_save_dir = os.path.join(config["base_dir"], 'evals', datetime.now().ctime().replace('  ', ' ').replace(' ', '_').replace(':', '_'))
Path(base_save_dir).mkdir(parents=True)


def input2discriminator(A_embs, B_embs, emb_d_out_shape, A_segs, B_segs, seg_d_out_shape):
    emb_d_x_batch = np.concatenate((A_embs, B_embs), axis=0)
    # A : 1, B : 0
    emb_d_y_batch = np.clip(np.random.uniform(0.85, 0.95, size=[emb_d_x_batch.shape[0]] + list(emb_d_out_shape)[1:]), a_min=0, a_max=1)
    emb_d_y_batch[A_embs.shape[0]:, ...] = 1 - emb_d_y_batch[A_embs.shape[0]:, ...]

    seg_d_x_batch = np.concatenate((A_segs, B_segs), axis=0)
    # A : 1, B : 0
    seg_d_y_batch = np.clip(np.random.uniform(0.85, 0.95, size=[seg_d_x_batch.shape[0]] + list(seg_d_out_shape)[1:]), a_min=0, a_max=1)
    seg_d_y_batch[A_segs.shape[0]:, ...] = 1 - seg_d_y_batch[A_segs.shape[0]:, ...]

    return [emb_d_x_batch, seg_d_x_batch], [emb_d_y_batch, seg_d_y_batch]


def input2gan(patchesA, segsA, patchesB, d_out_shape):
    g_x_batch = [patchesA, patchesB]
    # set 1 to all labels (real : 1, fake : 0)
    g_y_batch = [
        segsA,
        # valid seg B
        np.clip(np.random.uniform(0.85, 0.95, size=[patchesB.shape[0]] + list(d_out_shape[1])[1:]), a_min=0, a_max=1),
        # valid emb A
        np.clip(np.random.uniform(0.05, 0.15, size=[patchesA.shape[0]] + list(d_out_shape[0])[1:]), a_min=0, a_max=1),
        # valid emb B
        np.clip(np.random.uniform(0.85, 0.95, size=[patchesB.shape[0]] + list(d_out_shape[0])[1:]), a_min=0, a_max=1)
    ]
    return g_x_batch, g_y_batch


def main(overwrite=False):
    # convert input images into an hdf5 file
    data_file_path = config['base_dir'] + '/{}_fetal_data.h5'

    A_data_file_path = data_file_path.format('A')
    B_data_file_path = data_file_path.format('B')
    if overwrite or (not os.path.exists(A_data_file_path)) or (not os.path.exists(B_data_file_path)):
        configA = config.copy()
        # configA['scans_dir'] = ''
        configA['data_file'] = A_data_file_path
        create_data_file(configA, name='A')

        # ugly patch
        configB = config.copy()
        configB['scans_dir'] = '../Datasets/Dixon_cut_windowed'
        configB['data_file'] = B_data_file_path
        create_data_file(configB, name='B')

    A_data_file_opened = open_data_file(A_data_file_path)
    B_data_file_opened = open_data_file(B_data_file_path)

    seg_loss_func = getattr(fetal_net.metrics, config['loss'])
    dis_loss_func = 'mse'  # getattr(fetal_net.metrics, config['dis_loss'])

    # instantiate seg model
    # segB_model_func = getattr(fetal_net.model, config['model_name'])
    # segB_model = segB_model_func(input_shape=config["input_shape"],
    #                              initial_learning_rate=config["initial_learning_rate"],
    #                              **{'dropout_rate': config['dropout_rate'],
    #                                 'loss_function': seg_loss_func,
    #                                 'mask_shape': None if config["weight_mask"] is None else config[
    #                                     "input_shape"],
    #                                 'old_model_path': config['old_model']})
    #
    from fetal_net.model.unet.ae import ae_model_2d
    encoder_model, decoder_model = ae_model_2d(input_shape=config["input_shape_seg"],
                                               dropout_rate=config['dropout_rate'])

    # dis_model_func = getattr(fetal_net.model, config['dis_model_name'])
    # dis_model = dis_model_func(
    #     input_shape=encoder_model.output_shape[1:],
    #     initial_learning_rate=config["initial_learning_rate"],
    #     **{'dropout_rate': config['dropout_rate'],
    #        'loss_function': dis_loss_func})
    def dis():
        inputs = Input(encoder_model.output_shape[1:])
        conv = LeakyReLU()(Conv2D(128, 1)(inputs))
        conv = LeakyReLU()(Conv2D(128, 4, strides=2, padding='same')(conv))
        conv = LeakyReLU()(Conv2D(128, 4, strides=2, padding='same')(conv))
        conv = LeakyReLU()(Conv2D(256, 2, strides=1, padding='valid')(conv))
        flat = Flatten()(conv)
        outputs = Dense(1, activation='sigmoid')(flat)
        dis_model = Model(inputs=[inputs], outputs=[outputs])
        dis_model.compile(Adam(lr=config["initial_learning_rate"]),
                          loss=dis_loss_func,
                          metrics=['mae'])
        return dis_model

    emb_dis_model = dis()
    emb_dis_model.summary()

    encoder_model.summary()
    decoder_model.summary()

    inputs_A = Input(shape=config["input_shape_seg"])
    inputs_B = Input(shape=config["input_shape_seg"])

    emb_A = encoder_model(inputs_A)
    seg_A = Activation('linear', name='sA')(decoder_model(emb_A))

    emb_B = encoder_model(inputs_B)
    seg_B = Activation('linear', name='sB')(decoder_model(emb_B))

    # seg
    seg_model = Model(inputs=[inputs_A], outputs=[seg_A])
    seg_model.compile(Adam(lr=config["initial_learning_rate"]), loss=seg_loss_func)

    if not overwrite \
            and len(glob.glob(config["model_file"] + 'seg_*.h5')) > 0:
        seg_model_path = get_last_model_path(config["model_file"] + 'seg_')
        print('Loading segB model from: {}'.format(seg_model_path))
        seg_model.load_weights(seg_model_path)

    seg_model.summary()

    from fetal_net.model.discriminator import PatchDiscriminator
    seg_dis_model = PatchDiscriminator.build_discriminator_2d(input_shape=seg_model.output_shape[1:])
    seg_dis_model.summary()

    combined_dis_model = Model(inputs=[emb_dis_model.input, seg_dis_model.input],
                               outputs=[Activation('linear', name='sE')(emb_dis_model.output), Activation('linear', name='sD')(seg_dis_model.output)])
    combined_dis_model.compile(loss=['mse', 'mse'],
                               loss_weights=[config["gdE_loss_ratio"], config["gdS_loss_ratio"]],
                               metrics=['mae', 'mae'],
                               optimizer=Adam(config["initial_learning_rate"]))
    combined_dis_model.summary()

    # Build "frozen discriminator"
    frozen_dis_model = Network(
        emb_dis_model.inputs,
        emb_dis_model.outputs,
        name='frozen_discriminator'
    )
    frozen_dis_model.trainable = False

    # Build "frozen discriminator"
    frozen_seg_dis_model = Network(
        seg_dis_model.inputs,
        seg_dis_model.outputs,
        name='frozen_seg_discriminator'
    )
    frozen_seg_dis_model.trainable = False

    # dis
    valid_eA = Activation('linear', name='dEA')(frozen_dis_model(emb_A))
    valid_eB = Activation('linear', name='dEB')(frozen_dis_model(emb_B))
    valid_sB = Activation('linear', name='dSB')(frozen_seg_dis_model(seg_B))

    # inputs_A = Input(shape=config["input_shape_seg"])
    # seg_A = Activation(None, name='A_seg')(seg_model(inputs_A))
    combined_model = Model(inputs=[inputs_A, inputs_B],
                           outputs=[seg_A, valid_sB, valid_eA, valid_eB])
    combined_model.compile(loss=[seg_loss_func, 'mse', 'mse', 'mse'],
                           loss_weights=[1, config["gdS_loss_ratio"], config["gdE_loss_ratio"], config["gdE_loss_ratio"]],
                           metrics={'dSB': ['mae'], 'dEA': ['mae'], 'dEB': ['mae']},
                           optimizer=Adam(config["initial_learning_rate"]))
    combined_model.summary()

    data_params = dict(batch_size=config["batch_size"],
                       data_split=config["validation_split"],
                       overwrite=overwrite,
                       validation_keys_file=config["validation_file"],
                       training_keys_file=config["training_file"],
                       test_keys_file=config["test_file"],
                       n_labels=config["n_labels"],
                       labels=config["labels"],
                       patch_shape=(*config["patch_shape"], config["patch_depth"]),
                       validation_batch_size=config["validation_batch_size"],
                       augment=config["augment"],
                       skip_blank_train=config["skip_blank_train"],
                       skip_blank_val=config["skip_blank_val"],
                       truth_index=config["truth_index"],
                       truth_size=config["truth_size"],
                       prev_truth_index=config["prev_truth_index"],
                       prev_truth_size=config["prev_truth_size"],
                       truth_downsample=config["truth_downsample"],
                       truth_crop=config["truth_crop"],
                       patches_per_epoch=config["patches_per_epoch"],
                       categorical=config["categorical"], is3d=config["3D"],
                       drop_easy_patches_train=config["drop_easy_patches_train"],
                       drop_easy_patches_val=config["drop_easy_patches_val"])
    # get training and testing generators
    A_train_generator, A_validation_generator, n_train_steps, n_validation_steps = get_training_and_validation_generators(
        A_data_file_opened,
        **data_params)

    split_dirB = './split_dix'
    data_params["training_keys_file"] = os.path.join(split_dirB, "training_ids.pkl")
    data_params["validation_keys_file"] = os.path.join(split_dirB, "validation_ids.pkl")
    data_params["test_keys_file"] = os.path.join(split_dirB, "test_ids.pkl")
    # get training and testing generators
    B_train_generator, B_validation_generator, n_train_steps, n_validation_steps = get_training_and_validation_generators(
        B_data_file_opened,
        **data_params)

    # start training
    scheduler = Scheduler(config["dis_steps"], config["gen_steps"],
                          init_lr=config["initial_learning_rate"],
                          lr_patience=config["patience"],
                          lr_decay=config["learning_rate_drop"])

    def get_it(arr, inds):
        return [arr[i] for i in inds]


    best_loss = np.inf
    for epoch in range(config["n_epochs"]):
        postfix = {}  # , 'val_g': None, 'val_d': None}
        with tqdm(range(n_train_steps // config["gen_steps"]), dynamic_ncols=True) as pbar:
            for n_round in pbar:
                # train D
                outputs = np.zeros(combined_dis_model.metrics_names.__len__())
                for i in range(scheduler.get_dsteps()):
                    A_patches, _ = next(A_train_generator)
                    B_patches, _ = next(B_train_generator)
                    d_x_batch, d_y_batch = \
                        input2discriminator(encoder_model.predict(A_patches, batch_size=config["batch_size"]),
                                            encoder_model.predict(B_patches, batch_size=config["batch_size"]),
                                            emb_dis_model.output_shape,
                                            seg_model.predict(A_patches, batch_size=config["batch_size"]),
                                            seg_model.predict(B_patches, batch_size=config["batch_size"]),
                                            seg_dis_model.output_shape)
                    outputs += combined_dis_model.train_on_batch(d_x_batch, d_y_batch)
                if scheduler.get_dsteps():
                    outputs /= scheduler.get_dsteps()
                    dsc_inds_d = [0,1,-1,-2,-3,-4]
                    postfix['d'] = build_dsc(get_it(combined_dis_model.metrics_names, dsc_inds_d), get_it(outputs, dsc_inds_d))
                    pbar.set_postfix(**postfix)

                # train G (freeze discriminator)
                outputs = np.zeros(combined_model.metrics_names.__len__())
                for i in range(scheduler.get_gsteps()):
                    A_patches, A_segs = next(A_train_generator)
                    B_patches, _ = next(B_train_generator)
                    g_x_batch, g_y_batch = input2gan(A_patches, A_segs, B_patches,
                                                     combined_dis_model.output_shape)
                    outputs += combined_model.train_on_batch(g_x_batch, g_y_batch)
                outputs /= scheduler.get_gsteps()

                dsc_inds = [0, 1, -1, -2, -3]


                postfix['g'] = build_dsc(get_it(combined_model.metrics_names, dsc_inds),
                                         get_it(outputs, dsc_inds))
                pbar.set_postfix(**postfix)

            # evaluate on validation set
            dis_metrics = np.zeros(combined_dis_model.metrics_names.__len__(), dtype=float)
            gen_metrics = np.zeros(seg_model.metrics_names.__len__(), dtype=float)
            evaluation_rounds = n_validation_steps
            for n_round in range(evaluation_rounds):  # rounds_for_evaluation:
                A_val_patches, A_val_segs = next(A_validation_generator)
                B_val_patches, _ = next(B_train_generator)

                # D
                if scheduler.get_dsteps() > 0:
                    d_x_test, d_y_test = \
                        input2discriminator(encoder_model.predict(A_val_patches, batch_size=config["validation_batch_size"]),
                                            encoder_model.predict(B_val_patches, batch_size=config["validation_batch_size"]),
                                            emb_dis_model.output_shape,
                                            seg_model.predict(A_val_patches, batch_size=config["validation_batch_size"]),
                                            seg_model.predict(B_val_patches, batch_size=config["validation_batch_size"]),
                                            seg_dis_model.output_shape)
                    dis_metrics += combined_dis_model.evaluate(d_x_test, d_y_test,
                                                               batch_size=config["validation_batch_size"],
                                                               verbose=0)

                # G
                # gen_x_test, gen_y_test = input2gan(val_patches, val_segs, dis_model.output_shape)
                gen_metrics += seg_model.evaluate(A_val_patches, A_val_segs,
                                                  batch_size=config["validation_batch_size"],
                                                  verbose=0)

                # only save for one round of evaluation
                if n_round == 0 and config["save_evals"] > 0:
                    # save seg examples
                    B_val_pred = seg_model.predict(B_val_patches[:config["save_evals"]])
                    for i, (pred, patch) in enumerate(zip(B_val_pred, B_val_patches)):
                        save_dir = os.path.join(base_save_dir, str(epoch))
                        Path(save_dir).mkdir(parents=False, exist_ok=True)
                        nib.save(nib.Nifti1Pair(patch[..., config["truth_index"]], np.eye(4)), os.path.join(save_dir, str(i) + '_patch.nii.gz'))
                        nib.save(nib.Nifti1Pair(pred[..., 0], np.eye(4)), os.path.join(save_dir, str(i) + '_seg.nii.gz'))

            dis_metrics /= float(evaluation_rounds)
            gen_metrics /= float(evaluation_rounds)
            # save the model and weights with the best validation loss
            if gen_metrics[0] + dis_metrics[0] < best_loss:
                best_loss = gen_metrics[0] + dis_metrics[0]
                print('Saving Model...')
                with open(os.path.join(config["base_dir"], "seg_{}_g{:.3f}_d{:.3f}.json".format(epoch, gen_metrics[0], dis_metrics[0])), 'w') as f:
                    f.write(seg_model.to_json())
                seg_model.save_weights(os.path.join(config["base_dir"], "seg_{}_g{:.3f}_d{:.3f}.h5".format(epoch, gen_metrics[0], dis_metrics[0])))

            postfix['val_d'] = build_dsc(combined_dis_model.metrics_names, dis_metrics)
            postfix['val_g'] = build_dsc(seg_model.metrics_names, gen_metrics)
            # pbar.set_postfix(**postfix)
            print('val_d: ' + postfix['val_d'], end=' | ')
            print('val_g: ' + postfix['val_g'])
            # pbar.refresh()

            # update step sizes, learning rates
            scheduler.update_steps(epoch, gen_metrics[0])
            K.set_value(combined_dis_model.optimizer.lr, scheduler.get_lr())
            K.set_value(combined_model.optimizer.lr, scheduler.get_lr())

    A_data_file_opened.close()
    B_data_file_opened.close()


if __name__ == "__main__":
    main(overwrite=config["overwrite"])
