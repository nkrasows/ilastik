#ifndef CONTEXTAVERAGE_HXX
#define CONTEXTAVERAGE_HXX

#include <iostream>
#include <vector>
#include <limits>

#include <vigra/multi_array.hxx>
#include <vigra/numpy_array.hxx>
#include <vigra/numpy_array_converters.hxx>
#include <vigra/multi_pointoperators.hxx>
#include <vigra/utilities.hxx>

#include "integralImage.hxx"

using namespace vigra;
//using namespace std;

template <class IND, class T, class S1, class S2>
void average_features(MultiArrayView<1, IND, S1>& radii,
                        IND x, IND y, IND c, 
                        MultiArrayView<3, T, S2>& integral,
                        std::vector<T>& averages)
{
    int nr = radii.size();
    int nclasses = integral.shape()[2];
    //std::cout<<"predictions shape: "<<predictions.shape()[0]<<" "<<predictions.shape()[1]<<" "<<predictions.shape()[2]<<std::endl;
    for (int ir=0; ir<nr; ++ir){
        
        IND xminus = 0;
        IND yminus = 0;
        if (x<radii[ir] || y<radii[ir] || x+radii[ir]>integral.shape()[0]-1 || y+radii[ir]>integral.shape()[1]-1){
            averages[ir]= 1./nclasses;
            continue;
        } 
        
        //std::cout<<"filling with non-default"<<std::endl;
        
        T ul = (x==radii[ir] || y==radii[ir]) ? 0 : integral(x-radii[ir]-1, y-radii[ir]-1, c);
        T ll = (y==radii[ir]) ? 0 : integral(x+radii[ir], y-radii[ir]-1, c);
        T ur = (x==radii[ir]) ? 0 : integral(x-radii[ir]-1, y+radii[ir], c);
        T lr = integral(x+radii[ir], y+radii[ir], c);
        
        T sum = lr-ll-ur+ul;
        int n = (2*radii[ir]+1)*(2*radii[ir]+1);
        if (ir>0){
            T sum_prev = averages[ir-1]*(2*radii[ir-1]+1)*(2*radii[ir-1]+1);
            sum-=sum_prev;
            n-=(2*radii[ir-1]+1)*(2*radii[ir-1]+1);
        }
        averages[ir]=sum/n;

    }
    return;
}

template <class IND, class T, class S1, class S2>
void average_features_3d_is(MultiArrayView<1, IND, S1>& radii,
                            IND x, IND y, IND z, IND c,
                            MultiArrayView<4, T, S2>& integral,
                            std::vector<T>& averages)
{
    //FIXME: sizes are isotropic for now, anisotropic sizes later
    /* (0, 0, 0) is at d
      b_____a
    d_|__c  |
    | |  |  |
    | f__|__e
    h____g
    volume = e-a-f-g+b+c+h-d
    */
    
    int nx = integral.shape()[0];
    int ny = integral.shape()[1];
    int nz = integral.shape()[2];
    int nr = radii.size();
    int nclasses = integral.shape()[3];
    for (int ir=0; ir<nr; ++ir){
        //FIXME: do a better border treatment
        if (x<radii[ir] || y<radii[ir] || z<radii[ir] || x+radii[ir]>nx-1 || y+radii[ir]>ny-1 || z+radii[ir]>nz-1){
            averages[ir] = 1./nclasses;
            continue;
        }
        if (x==2 && y==2 && z==2 && c==0) {
            std::cout<<"out of border condition"<<std::endl;
        }
        
        T uul = (x==radii[ir] || y==radii[ir] || z==radii[ir]) ? 0 : integral(x-radii[ir]-1, y-radii[ir]-1, z-radii[ir]-1, c);
        T ull = (y==radii[ir] || z==radii[ir]) ? 0 : integral(x+radii[ir], y-radii[ir]-1, z-radii[ir]-1, c);
        T uur = (x==radii[ir] || z==radii[ir]) ? 0 : integral(x-radii[ir]-1, y+radii[ir], z-radii[ir]-1, c);
        T ulr = (z==radii[ir]) ? 0 : integral(x+radii[ir], y+radii[ir], z-radii[ir]-1, c);
        
        T lul = (x==radii[ir] || y==radii[ir]) ? 0 : integral(x-radii[ir]-1, y-radii[ir]-1, z+radii[ir], c);
        T lll = (y==radii[ir]) ? 0 : integral(x+radii[ir], y-radii[ir]-1, z+radii[ir], c);
        T lur = (x==radii[ir]) ? 0 : integral(x-radii[ir]-1, y+radii[ir], z+radii[ir], c);
        T llr = integral(x+radii[ir], y+radii[ir], z+radii[ir], c);
        
        //T sum = lr-ll-ur+ul;
        //T sum = ulr - uur - ull - llr + uul + lur + lll - lul;
        T sum = uur + ull + llr + lul - ulr - uul - lur -lll;
        
        int n = (2*radii[ir]+1)*(2*radii[ir]+1)*(2*radii[ir]+1);
        if (x==2 && y==2 && z==2 && c==0) {
            std::cout<<"sum="<<sum<<" , n="<<n<<std::endl;
        }
        
        if (ir>0){
            int n_prev = (2*radii[ir-1]+1)*(2*radii[ir-1]+1)*(2*radii[ir-1]+1);
            T sum_prev = averages[ir-1]*n_prev;
            sum-=sum_prev;
            n-=n_prev;
        }
        averages[ir]=sum/n;
        if (x==2 && y==2 && z==2 && c==0) {
            std::cout<<"ir="<<ir<<" ,averages[ir]="<<averages[ir]<<std::endl;
        }
        
    }
        
    return;
    
}
                         
template <class IND, class T, class S>
void avContext2Dmulti(MultiArrayView<1, IND, S>& sizes,
                      MultiArrayView<3, T, S>& predictions,
                      MultiArrayView<3, T, S>& res)
{
    //fill the results array with averages of predictions array
    //computed at the radii of sizes of each member of predictions
    //the averages are computed in concentric squares of size radii,
    //where  for each r_i the middle of the square of size r_i-1 is removed
    
    int nx = predictions.shape()[0];
    int ny = predictions.shape()[1];
    int nclasses = predictions.shape()[2];
    int nnewfeatures = sizes.size();
    MultiArray<3, T> integral(predictions.shape());
    
    integralImage(predictions, integral);
    
    for (IND c=0; c<nclasses; ++c) {
//         std::cout<<"class "<<c<<std::endl;
        for (IND x=0; x<nx; ++x){
            for (IND y=0; y<ny; ++y){
                std::vector<T> newf(nnewfeatures);
                average_features(sizes, x, y, c, integral, newf);
                
                for (IND ii=0; ii<nnewfeatures; ++ii){
                    res(x, y, c*nnewfeatures + ii) = newf[ii];
                }
            }
        }
    }
    return;
}

template <class IND, class T, class S>
void varContext2Dmulti(MultiArrayView<1, IND, S>& sizes,
                       MultiArrayView<3, T, S>& predictions,
                       MultiArrayView<3, T, S>& res)
{
    //fill the results array with averages and variances of predictions array
    //computed at the radii of sizes of each member of predictions
    
    int nx = predictions.shape()[0];
    int ny = predictions.shape()[1];
    int nclasses = predictions.shape()[2];
    
    int nnewfeatures = sizes.size();
    
    MultiArray<3, T> integral(predictions.shape());
    MultiArray<3, T> integral2(predictions.shape());
    
    integralImage(predictions, integral);
    integralImage2(predictions, integral2);

    for (IND c=0; c<nclasses; ++c) {
//         std::cout<<"class "<<c<<std::endl;
        for (IND x=0; x<nx; ++x) {
            for (IND y=0; y<ny; ++y) {
                std::vector<T> newf(nnewfeatures);
                average_features(sizes, x, y, c, integral, newf);
                std::vector<T> newf2(nnewfeatures);
                average_features(sizes, x, y, c, integral2, newf2);
                //fill the averages
                for (IND ii=0; ii<nnewfeatures; ++ii) {
                    
                    res(x, y, c*2*nnewfeatures+ii) = newf[ii];
                }
                //fill the variances
                for (IND ii=0; ii<nnewfeatures; ++ii) {
                    res(x, y, c*2*nnewfeatures+nnewfeatures+ii) = newf2[ii]-newf[ii]*newf[ii];
                }
            }
        }
    }
    return;
}

template <class IND, class T, class S>
void varContext3Dmulti(MultiArrayView<1, IND, S>& sizes,
                       MultiArrayView<4, T, S>& predictions,
                       MultiArrayView<4, T, S>& res)
{
    int nx = predictions.shape()[0];
    int ny = predictions.shape()[1];
    int nz = predictions.shape()[2];
    int nclasses = predictions.shape()[3];
    
    int nnewfeatures = sizes.size();
    MultiArray<4, T> integral(predictions.shape());
    MultiArray<4, T> integral2(predictions.shape());
    
    integralVolume(predictions, integral);
    integralVolume2(predictions, integral2);
    
    for (IND c=0; c<nclasses; ++c){
        for (IND x=0; x<nx; ++x) {
            for (IND y=0; y<ny; ++y){
                for (IND z=0; z<nz; ++z){
                    std::vector<T> newf(nnewfeatures);
                    average_features_3d_is(sizes, x, y, z, c, integral, newf);
                    std::vector<T> newf2(nnewfeatures);
                    average_features_3d_is(sizes, x, y, z, c, integral2, newf2);
                    //fill the averages
                    for (IND ii=0; ii<nnewfeatures; ++ii){
                        if (x==2 && y==2 && z==2 && c==0) {
                            std::cout<<"ii="<<ii<<" , newf[ii]="<<newf[ii]<<std::endl;
                        }
                        res(x, y, z, c*2*nnewfeatures+ii) = newf[ii];
                    }
                    //fill the variances
                    for (IND ii=0; ii<nnewfeatures; ++ii) {
                        res(x, y, z, c*2*nnewfeatures+nnewfeatures+ii) = newf2[ii]-newf[ii]*newf[ii];
                    }
                }
            }
        }
    }
    return;
    
    
    
}

#endif